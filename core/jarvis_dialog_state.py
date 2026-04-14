#!/usr/bin/env python3
"""
jarvis_dialog_state — Dialog state machine for multi-turn conversations
Tracks conversation phase, slot filling, intent history, context carry-over
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.dialog_state")


class DialogPhase(str, Enum):
    GREETING = "greeting"
    INTENT_CAPTURE = "intent_capture"
    SLOT_FILLING = "slot_filling"
    CONFIRMATION = "confirmation"
    EXECUTION = "execution"
    CLARIFICATION = "clarification"
    ERROR_RECOVERY = "error_recovery"
    CLOSING = "closing"
    IDLE = "idle"


class SlotStatus(str, Enum):
    EMPTY = "empty"
    FILLED = "filled"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass
class SlotDef:
    name: str
    description: str
    required: bool = True
    prompt: str = ""  # question to ask user
    default: Any = None
    validator: str = ""  # e.g. "int", "email", "nonempty"


@dataclass
class SlotValue:
    slot: SlotDef
    status: SlotStatus = SlotStatus.EMPTY
    value: Any = None
    raw: str = ""
    filled_at: float = 0.0

    def fill(self, value: Any, raw: str = ""):
        self.value = value
        self.raw = raw
        self.status = SlotStatus.FILLED
        self.filled_at = time.time()

    def confirm(self):
        self.status = SlotStatus.CONFIRMED

    def reject(self):
        self.status = SlotStatus.REJECTED
        self.value = None

    def to_dict(self) -> dict:
        return {
            "slot": self.slot.name,
            "status": self.status.value,
            "value": self.value,
        }


@dataclass
class DialogIntent:
    name: str
    description: str = ""
    slots: list[SlotDef] = field(default_factory=list)
    requires_confirmation: bool = False


@dataclass
class TurnRecord:
    turn_id: int
    role: str  # "user" | "assistant"
    text: str
    intent: str = ""
    phase: DialogPhase = DialogPhase.IDLE
    ts: float = field(default_factory=time.time)
    entities: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "role": self.role,
            "text": self.text[:200],
            "intent": self.intent,
            "phase": self.phase.value,
            "ts": self.ts,
        }


@dataclass
class DialogContext:
    """Mutable context carried across turns."""

    session_id: str
    user_id: str = ""
    current_intent: str = ""
    phase: DialogPhase = DialogPhase.IDLE
    slots: dict[str, SlotValue] = field(default_factory=dict)
    history: list[TurnRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    turn_count: int = 0
    error_count: int = 0

    def touch(self):
        self.updated_at = time.time()

    def add_turn(self, role: str, text: str, **kwargs) -> TurnRecord:
        self.turn_count += 1
        turn = TurnRecord(
            turn_id=self.turn_count,
            role=role,
            text=text,
            phase=self.phase,
            intent=self.current_intent,
            **kwargs,
        )
        self.history.append(turn)
        self.touch()
        return turn

    def get_slot(self, name: str) -> SlotValue | None:
        return self.slots.get(name)

    def unfilled_required_slots(self) -> list[SlotValue]:
        return [
            sv
            for sv in self.slots.values()
            if sv.slot.required and sv.status == SlotStatus.EMPTY
        ]

    def all_required_filled(self) -> bool:
        return len(self.unfilled_required_slots()) == 0

    def slot_summary(self) -> dict:
        return {name: sv.to_dict() for name, sv in self.slots.items()}

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "phase": self.phase.value,
            "current_intent": self.current_intent,
            "turn_count": self.turn_count,
            "slots": self.slot_summary(),
            "history_len": len(self.history),
            "error_count": self.error_count,
        }


class DialogStateMachine:
    """
    Manages dialog state transitions.
    Handles slot filling lifecycle, phase transitions, confirmation flows.
    """

    # Valid phase transitions
    _TRANSITIONS: dict[DialogPhase, set[DialogPhase]] = {
        DialogPhase.IDLE: {DialogPhase.GREETING, DialogPhase.INTENT_CAPTURE},
        DialogPhase.GREETING: {DialogPhase.INTENT_CAPTURE, DialogPhase.IDLE},
        DialogPhase.INTENT_CAPTURE: {
            DialogPhase.SLOT_FILLING,
            DialogPhase.CONFIRMATION,
            DialogPhase.EXECUTION,
            DialogPhase.CLARIFICATION,
        },
        DialogPhase.SLOT_FILLING: {
            DialogPhase.CONFIRMATION,
            DialogPhase.EXECUTION,
            DialogPhase.CLARIFICATION,
            DialogPhase.ERROR_RECOVERY,
        },
        DialogPhase.CONFIRMATION: {
            DialogPhase.EXECUTION,
            DialogPhase.SLOT_FILLING,
            DialogPhase.INTENT_CAPTURE,
        },
        DialogPhase.EXECUTION: {
            DialogPhase.CLOSING,
            DialogPhase.INTENT_CAPTURE,
            DialogPhase.ERROR_RECOVERY,
        },
        DialogPhase.CLARIFICATION: {
            DialogPhase.INTENT_CAPTURE,
            DialogPhase.SLOT_FILLING,
            DialogPhase.ERROR_RECOVERY,
        },
        DialogPhase.ERROR_RECOVERY: {
            DialogPhase.INTENT_CAPTURE,
            DialogPhase.SLOT_FILLING,
            DialogPhase.CLOSING,
        },
        DialogPhase.CLOSING: {DialogPhase.IDLE},
    }

    def __init__(self):
        self._intents: dict[str, DialogIntent] = {}
        self._sessions: dict[str, DialogContext] = {}
        self._stats: dict[str, int] = {
            "sessions": 0,
            "turns": 0,
            "transitions": 0,
            "errors": 0,
            "completions": 0,
        }

    def register_intent(self, intent: DialogIntent):
        self._intents[intent.name] = intent

    def create_session(self, session_id: str, user_id: str = "") -> DialogContext:
        ctx = DialogContext(session_id=session_id, user_id=user_id)
        self._sessions[session_id] = ctx
        self._stats["sessions"] += 1
        log.info(f"Session created: {session_id}")
        return ctx

    def get_session(self, session_id: str) -> DialogContext | None:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str, user_id: str = "") -> DialogContext:
        return self._sessions.get(session_id) or self.create_session(
            session_id, user_id
        )

    def transition(
        self, ctx: DialogContext, to_phase: DialogPhase, reason: str = ""
    ) -> bool:
        allowed = self._TRANSITIONS.get(ctx.phase, set())
        if to_phase not in allowed:
            log.warning(
                f"[{ctx.session_id}] Invalid transition {ctx.phase} → {to_phase}"
            )
            return False
        old = ctx.phase
        ctx.phase = to_phase
        ctx.touch()
        self._stats["transitions"] += 1
        log.debug(f"[{ctx.session_id}] {old} → {to_phase} ({reason})")
        return True

    def set_intent(self, ctx: DialogContext, intent_name: str) -> bool:
        intent = self._intents.get(intent_name)
        if not intent:
            log.warning(f"Unknown intent: {intent_name!r}")
            return False
        ctx.current_intent = intent_name
        # Initialize slots
        ctx.slots = {
            s.name: SlotValue(slot=s, status=SlotStatus.EMPTY) for s in intent.slots
        }
        ctx.touch()
        return True

    def fill_slot(
        self, ctx: DialogContext, slot_name: str, value: Any, raw: str = ""
    ) -> bool:
        sv = ctx.slots.get(slot_name)
        if not sv:
            return False
        if not self._validate_slot(sv.slot, value):
            sv.status = SlotStatus.REJECTED
            ctx.error_count += 1
            return False
        sv.fill(value, raw)
        return True

    def _validate_slot(self, slot: SlotDef, value: Any) -> bool:
        if slot.validator == "nonempty":
            return bool(value)
        if slot.validator == "int":
            try:
                int(value)
                return True
            except (ValueError, TypeError):
                return False
        if slot.validator == "email":
            import re

            return bool(re.match(r"[^@]+@[^@]+\.[^@]+", str(value)))
        # No validator or unknown — accept
        return True

    def next_prompt(self, ctx: DialogContext) -> str | None:
        """Return prompt for next unfilled required slot, or None if all filled."""
        for sv in ctx.unfilled_required_slots():
            return sv.slot.prompt or f"Please provide {sv.slot.description}."
        return None

    def advance(self, ctx: DialogContext) -> DialogPhase:
        """Auto-advance phase based on slot fill state."""
        intent = self._intents.get(ctx.current_intent)

        if ctx.phase == DialogPhase.INTENT_CAPTURE:
            if not ctx.current_intent:
                return ctx.phase
            if ctx.slots:
                self.transition(ctx, DialogPhase.SLOT_FILLING, "slots needed")
            elif intent and intent.requires_confirmation:
                self.transition(ctx, DialogPhase.CONFIRMATION, "no slots, confirm")
            else:
                self.transition(ctx, DialogPhase.EXECUTION, "no slots, execute")

        elif ctx.phase == DialogPhase.SLOT_FILLING:
            if ctx.all_required_filled():
                if intent and intent.requires_confirmation:
                    self.transition(ctx, DialogPhase.CONFIRMATION, "all slots filled")
                else:
                    self.transition(ctx, DialogPhase.EXECUTION, "ready")

        return ctx.phase

    def complete(self, ctx: DialogContext):
        self.transition(ctx, DialogPhase.CLOSING, "done")
        self._stats["completions"] += 1

    def reset_intent(self, ctx: DialogContext):
        ctx.current_intent = ""
        ctx.slots = {}
        ctx.error_count = 0
        self.transition(ctx, DialogPhase.INTENT_CAPTURE, "reset")

    def process_turn(
        self,
        session_id: str,
        user_text: str,
        detected_intent: str = "",
        slot_updates: dict[str, Any] | None = None,
    ) -> dict:
        """
        High-level turn processor.
        Returns dict with current phase, next_prompt, and context summary.
        """
        ctx = self.get_or_create(session_id)
        ctx.add_turn("user", user_text)
        self._stats["turns"] += 1

        # Set intent if detected
        if detected_intent and detected_intent != ctx.current_intent:
            self.set_intent(ctx, detected_intent)
            if ctx.phase in (DialogPhase.IDLE, DialogPhase.GREETING):
                ctx.phase = DialogPhase.INTENT_CAPTURE

        # Fill slots
        if slot_updates:
            for slot_name, value in slot_updates.items():
                ok = self.fill_slot(ctx, slot_name, value, raw=str(value))
                if not ok:
                    log.debug(f"Slot {slot_name!r} rejected value {value!r}")

        # Auto-advance
        self.advance(ctx)

        next_prompt = self.next_prompt(ctx)

        return {
            "session_id": session_id,
            "phase": ctx.phase.value,
            "intent": ctx.current_intent,
            "next_prompt": next_prompt,
            "all_slots_filled": ctx.all_required_filled(),
            "slots": ctx.slot_summary(),
            "turn_count": ctx.turn_count,
        }

    def close_session(self, session_id: str):
        if session_id in self._sessions:
            del self._sessions[session_id]

    def active_sessions(self) -> int:
        return len(self._sessions)

    def stats(self) -> dict:
        return {**self._stats, "active_sessions": self.active_sessions()}


def build_jarvis_dialog_state() -> DialogStateMachine:
    dsm = DialogStateMachine()

    dsm.register_intent(
        DialogIntent(
            name="cluster_health",
            description="Check cluster health status",
            slots=[
                SlotDef(
                    "node",
                    "Node name (m1/m2/ol1)",
                    required=False,
                    prompt="Which node? (m1, m2, ol1, or leave blank for all)",
                    default="all",
                ),
            ],
            requires_confirmation=False,
        )
    )

    dsm.register_intent(
        DialogIntent(
            name="model_inference",
            description="Run inference on a model",
            slots=[
                SlotDef(
                    "model",
                    "Model name",
                    required=True,
                    prompt="Which model do you want to use?",
                    validator="nonempty",
                ),
                SlotDef(
                    "prompt",
                    "Input prompt",
                    required=True,
                    prompt="What is your prompt?",
                    validator="nonempty",
                ),
                SlotDef(
                    "max_tokens",
                    "Max tokens",
                    required=False,
                    default=512,
                    validator="int",
                ),
            ],
            requires_confirmation=False,
        )
    )

    dsm.register_intent(
        DialogIntent(
            name="shutdown_node",
            description="Gracefully shut down a cluster node",
            slots=[
                SlotDef(
                    "node",
                    "Node to shut down",
                    required=True,
                    prompt="Which node should be shut down?",
                    validator="nonempty",
                ),
                SlotDef(
                    "reason", "Reason for shutdown", required=False, default="manual"
                ),
            ],
            requires_confirmation=True,
        )
    )

    return dsm


def main():
    import sys

    dsm = build_jarvis_dialog_state()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Dialog state machine demo...\n")

        # Simulate multi-turn for model_inference
        session = "sess_001"

        turns = [
            ("I want to run inference", "model_inference", {}),
            ("use qwen3.5", "", {"model": "qwen3.5"}),
            ("tell me about GPUs", "", {"prompt": "tell me about GPUs"}),
        ]

        for user_text, intent, slots in turns:
            result = dsm.process_turn(session, user_text, intent, slots)
            print(f"  User: {user_text!r}")
            print(f"  → phase={result['phase']} intent={result['intent']}")
            print(f"    next_prompt={result['next_prompt']!r}")
            print(f"    slots={result['slots']}")
            print()

        # Shutdown flow with confirmation
        print("--- Shutdown flow ---")
        session2 = "sess_002"
        r = dsm.process_turn(session2, "shutdown m1", "shutdown_node", {"node": "m1"})
        print(f"  phase={r['phase']} prompt={r['next_prompt']!r}")

        print(f"\nStats: {json.dumps(dsm.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(dsm.stats(), indent=2))


if __name__ == "__main__":
    main()
