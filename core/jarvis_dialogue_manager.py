#!/usr/bin/env python3
"""
jarvis_dialogue_manager — Multi-turn dialogue state machine with slot filling
Manages conversation flows, slot collection, and action dispatch
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.dialogue_manager")

REDIS_PREFIX = "jarvis:dlg:"


class SlotStatus(str, Enum):
    EMPTY = "empty"
    FILLED = "filled"
    CONFIRMED = "confirmed"
    INVALID = "invalid"


class FlowState(str, Enum):
    IDLE = "idle"
    COLLECTING = "collecting"  # waiting for slot values
    CONFIRMING = "confirming"  # asking user to confirm
    EXECUTING = "executing"  # running the action
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Slot:
    slot_id: str
    prompt: str  # what to ask the user
    required: bool = True
    value: Any = None
    status: SlotStatus = SlotStatus.EMPTY
    validator: Any = None  # callable(value) → bool
    choices: list[str] = field(default_factory=list)  # constrained choices
    default: Any = None

    def fill(self, value: Any) -> bool:
        if self.choices and str(value).lower() not in [c.lower() for c in self.choices]:
            self.status = SlotStatus.INVALID
            return False
        if self.validator and not self.validator(value):
            self.status = SlotStatus.INVALID
            return False
        self.value = value
        self.status = SlotStatus.FILLED
        return True

    def to_dict(self) -> dict:
        return {
            "slot_id": self.slot_id,
            "prompt": self.prompt,
            "required": self.required,
            "value": self.value,
            "status": self.status.value,
            "choices": self.choices,
        }


@dataclass
class DialogueFlow:
    flow_id: str
    name: str
    slots: list[Slot] = field(default_factory=list)
    confirm_template: str = "Confirm: {summary}? (yes/no)"
    require_confirmation: bool = False
    handler: Any = None  # async callable(slots_dict) → str
    description: str = ""

    def unfilled_slots(self) -> list[Slot]:
        return [s for s in self.slots if s.required and s.status == SlotStatus.EMPTY]

    def all_filled(self) -> bool:
        return not self.unfilled_slots()

    def slots_dict(self) -> dict[str, Any]:
        return {s.slot_id: s.value for s in self.slots}

    def fill_from_entities(self, entities: dict[str, list[str]]) -> int:
        """Try to fill slots from extracted entities. Returns count filled."""
        filled = 0
        for slot in self.slots:
            if slot.status != SlotStatus.EMPTY:
                continue
            # Try matching slot_id or related entity type
            for etype, values in entities.items():
                if slot.slot_id in etype or etype in slot.slot_id:
                    if values and slot.fill(values[0]):
                        filled += 1
                        break
        return filled


@dataclass
class DialogueSession:
    session_id: str
    conv_id: str = ""
    agent_id: str = ""
    state: FlowState = FlowState.IDLE
    current_flow_id: str = ""
    current_flow: DialogueFlow | None = None
    history: list[dict] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_response: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "conv_id": self.conv_id,
            "state": self.state.value,
            "flow": self.current_flow_id,
            "history_turns": len(self.history),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DialogueManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._flows: dict[str, DialogueFlow] = {}
        self._sessions: dict[str, DialogueSession] = {}
        self._intent_handlers: dict[str, str] = {}  # intent → flow_id
        self._fallback: Callable | None = None
        self._stats: dict[str, int] = {
            "sessions": 0,
            "turns": 0,
            "flows_completed": 0,
            "flows_cancelled": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_flow(self, flow: DialogueFlow):
        self._flows[flow.flow_id] = flow

    def map_intent(self, intent: str, flow_id: str):
        self._intent_handlers[intent] = flow_id

    def set_fallback(self, handler: Callable):
        self._fallback = handler

    def get_session(self, session_id: str) -> DialogueSession:
        if session_id not in self._sessions:
            s = DialogueSession(session_id=session_id)
            self._sessions[session_id] = s
            self._stats["sessions"] += 1
        return self._sessions[session_id]

    def _start_flow(self, session: DialogueSession, flow_id: str):
        flow = self._flows.get(flow_id)
        if not flow:
            return
        # Deep copy slots to avoid shared state between sessions
        import copy

        session.current_flow = copy.deepcopy(flow)
        session.current_flow_id = flow_id
        session.state = FlowState.COLLECTING

    async def process(
        self,
        session_id: str,
        user_input: str,
        intent: str = "",
        entities: dict[str, list[str]] | None = None,
        conv_id: str = "",
    ) -> str:
        session = self.get_session(session_id)
        session.conv_id = conv_id or session.conv_id
        session.updated_at = time.time()
        self._stats["turns"] += 1
        entities = entities or {}

        session.history.append(
            {"role": "user", "content": user_input, "ts": time.time()}
        )

        response = await self._route(session, user_input, intent, entities)
        session.last_response = response
        session.history.append(
            {"role": "assistant", "content": response, "ts": time.time()}
        )

        if self.redis:
            asyncio.create_task(self._redis_save(session))

        return response

    async def _route(
        self,
        session: DialogueSession,
        user_input: str,
        intent: str,
        entities: dict[str, list[str]],
    ) -> str:
        # Handle cancellation
        if user_input.lower().strip() in ("cancel", "abort", "stop", "quit"):
            if session.state != FlowState.IDLE:
                session.state = FlowState.CANCELLED
                session.current_flow = None
                self._stats["flows_cancelled"] += 1
                return "Action cancelled."

        # Active flow — continue collecting
        if session.current_flow and session.state == FlowState.COLLECTING:
            return await self._continue_flow(session, user_input, entities)

        # Confirmation step
        if session.state == FlowState.CONFIRMING:
            return await self._handle_confirm(session, user_input)

        # Start new flow from intent
        if intent and intent in self._intent_handlers:
            flow_id = self._intent_handlers[intent]
            self._start_flow(session, flow_id)
            if session.current_flow:
                session.current_flow.fill_from_entities(entities)
                return await self._continue_flow(session, user_input, entities)

        # Fallback
        if self._fallback:
            return await self._fallback(session, user_input, intent, entities)

        return f"I'm in {session.state.value} state. Try a specific command or intent."

    async def _continue_flow(
        self,
        session: DialogueSession,
        user_input: str,
        entities: dict[str, list[str]],
    ) -> str:
        flow = session.current_flow
        if not flow:
            return "No active flow."

        # Try to fill slots from entities or direct input
        flow.fill_from_entities(entities)

        unfilled = flow.unfilled_slots()
        if unfilled:
            # Fill current slot from user input
            current_slot = unfilled[0]
            if user_input.strip() and session.history:
                current_slot.fill(user_input.strip())
                unfilled = flow.unfilled_slots()

        # Still unfilled slots?
        if unfilled:
            slot = unfilled[0]
            prompt = slot.prompt
            if slot.choices:
                prompt += f" (options: {', '.join(slot.choices)})"
            if slot.default is not None:
                prompt += f" [default: {slot.default}]"
            return prompt

        # All slots filled — confirm or execute
        if flow.require_confirmation:
            session.state = FlowState.CONFIRMING
            summary = ", ".join(f"{k}={v}" for k, v in flow.slots_dict().items())
            return flow.confirm_template.format(summary=summary)

        return await self._execute_flow(session)

    async def _handle_confirm(self, session: DialogueSession, user_input: str) -> str:
        if user_input.lower().strip() in ("yes", "y", "confirm", "ok"):
            return await self._execute_flow(session)
        elif user_input.lower().strip() in ("no", "n", "cancel"):
            session.state = FlowState.CANCELLED
            session.current_flow = None
            self._stats["flows_cancelled"] += 1
            return "Action cancelled."
        return "Please confirm with 'yes' or 'no'."

    async def _execute_flow(self, session: DialogueSession) -> str:
        flow = session.current_flow
        if not flow:
            return "No active flow."

        session.state = FlowState.EXECUTING
        try:
            if flow.handler:
                if asyncio.iscoroutinefunction(flow.handler):
                    result = await flow.handler(flow.slots_dict())
                else:
                    result = flow.handler(flow.slots_dict())
            else:
                result = f"Flow '{flow.name}' executed with: {flow.slots_dict()}"

            session.state = FlowState.DONE
            session.current_flow = None
            self._stats["flows_completed"] += 1
            return str(result)
        except Exception as e:
            session.state = FlowState.FAILED
            return f"Error executing {flow.name}: {e}"

    async def _redis_save(self, session: DialogueSession):
        if not self.redis:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}{session.session_id}",
                3600,
                json.dumps(session.to_dict()),
            )
        except Exception:
            pass

    def stats(self) -> dict:
        return {
            **self._stats,
            "active_sessions": len(self._sessions),
            "registered_flows": len(self._flows),
        }


def build_jarvis_dialogue_manager() -> DialogueManager:
    mgr = DialogueManager()

    async def _run_query(slots: dict) -> str:
        model = slots.get("model", "qwen3.5-9b")
        query = slots.get("query", "")
        return f"[Inference] model={model} query={query!r}"

    async def _run_trading(slots: dict) -> str:
        symbol = slots.get("symbol", "BTC/USDT")
        action = slots.get("action", "analyze")
        return f"[Trading] {action} {symbol}"

    mgr.register_flow(
        DialogueFlow(
            flow_id="query_model",
            name="Query a model",
            slots=[
                Slot(
                    "model",
                    "Which model? (qwen3.5-9b, deepseek-r1, gemma3:4b)",
                    choices=["qwen3.5-9b", "deepseek-r1", "gemma3:4b"],
                ),
                Slot("query", "What is your query?"),
            ],
            handler=_run_query,
        )
    )
    mgr.register_flow(
        DialogueFlow(
            flow_id="trading_action",
            name="Trading action",
            slots=[
                Slot("symbol", "Which ticker? (BTC/USDT, ETH/USDT, SOL/USDT)"),
                Slot(
                    "action",
                    "Action? (analyze, signal, backtest)",
                    choices=["analyze", "signal", "backtest"],
                ),
            ],
            require_confirmation=True,
            handler=_run_trading,
        )
    )

    mgr.map_intent("query", "query_model")
    mgr.map_intent("trade", "trading_action")

    return mgr


async def main():
    import sys

    mgr = build_jarvis_dialogue_manager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Dialogue manager demo...")
        sid = "test-session"

        turns = [
            ("I want to query a model", "query", {}),
            ("qwen3.5-9b", "", {}),
            ("What is the capital of France?", "", {}),
        ]

        for text, intent, entities in turns:
            print(f"\n  User: {text!r}")
            resp = await mgr.process(sid, text, intent=intent, entities=entities)
            print(f"  Bot:  {resp!r}")
            sess = mgr.get_session(sid)
            print(f"  State: {sess.state.value}")

        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
