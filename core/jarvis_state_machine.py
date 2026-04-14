#!/usr/bin/env python3
"""
jarvis_state_machine — Generic finite state machine with guards, actions, and history
Used for model lifecycle, pipeline stages, session states, and agent behavior
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.state_machine")

REDIS_PREFIX = "jarvis:fsm:"


@dataclass
class Transition:
    from_state: str
    event: str
    to_state: str
    guard: Callable | None = None  # guard(context) → bool
    action: Callable | None = None  # action(context, event_data) → None
    description: str = ""

    def can_fire(self, context: dict) -> bool:
        if self.guard is None:
            return True
        try:
            return bool(self.guard(context))
        except Exception:
            return False


@dataclass
class StateEntry:
    state: str
    event: str
    from_state: str
    ts: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "event": self.event,
            "from_state": self.from_state,
            "ts": self.ts,
            "data": self.data,
        }


@dataclass
class FSMDefinition:
    name: str
    initial: str
    states: set[str]
    final_states: set[str] = field(default_factory=set)
    transitions: list[Transition] = field(default_factory=list)

    def add_transition(
        self,
        from_state: str,
        event: str,
        to_state: str,
        guard: Callable | None = None,
        action: Callable | None = None,
        description: str = "",
    ) -> "FSMDefinition":
        self.transitions.append(
            Transition(
                from_state=from_state,
                event=event,
                to_state=to_state,
                guard=guard,
                action=action,
                description=description,
            )
        )
        self.states.add(from_state)
        self.states.add(to_state)
        return self

    def get_transitions(self, from_state: str, event: str) -> list[Transition]:
        return [
            t
            for t in self.transitions
            if t.from_state == from_state and t.event == event
        ]


class StateMachineInstance:
    def __init__(
        self, instance_id: str, definition: FSMDefinition, context: dict | None = None
    ):
        self.instance_id = instance_id
        self.definition = definition
        self.current_state = definition.initial
        self.context: dict = context or {}
        self.history: list[StateEntry] = []
        self.created_at = time.time()
        self._state_entered_at = time.time()

    @property
    def is_final(self) -> bool:
        return self.current_state in self.definition.final_states

    @property
    def time_in_state_s(self) -> float:
        return round(time.time() - self._state_entered_at, 2)

    async def send(self, event: str, data: dict | None = None) -> tuple[bool, str]:
        """Send event. Returns (success, new_state or error_msg)."""
        transitions = self.definition.get_transitions(self.current_state, event)

        if not transitions:
            msg = f"No transition from '{self.current_state}' on event '{event}'"
            log.debug(f"FSM [{self.instance_id}]: {msg}")
            return False, msg

        # Pick first transition whose guard passes
        fired = None
        for t in transitions:
            if t.can_fire(self.context):
                fired = t
                break

        if not fired:
            msg = f"All guards blocked transition from '{self.current_state}' on '{event}'"
            log.debug(f"FSM [{self.instance_id}]: {msg}")
            return False, msg

        old_state = self.current_state
        entry = StateEntry(
            state=fired.to_state,
            event=event,
            from_state=old_state,
            data=data or {},
        )

        # Execute action
        if fired.action:
            try:
                if asyncio.iscoroutinefunction(fired.action):
                    await fired.action(self.context, data or {})
                else:
                    fired.action(self.context, data or {})
            except Exception as e:
                log.error(f"FSM [{self.instance_id}] action error: {e}")

        self.current_state = fired.to_state
        self._state_entered_at = time.time()
        self.history.append(entry)

        log.debug(
            f"FSM [{self.instance_id}]: {old_state} --[{event}]--> {self.current_state}"
        )
        return True, self.current_state

    def available_events(self) -> list[str]:
        return list(
            {
                t.event
                for t in self.definition.transitions
                if t.from_state == self.current_state and t.can_fire(self.context)
            }
        )

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "definition": self.definition.name,
            "current_state": self.current_state,
            "is_final": self.is_final,
            "time_in_state_s": self.time_in_state_s,
            "history_length": len(self.history),
            "available_events": self.available_events(),
        }


class StateMachineEngine:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._definitions: dict[str, FSMDefinition] = {}
        self._instances: dict[str, StateMachineInstance] = {}
        self._register_builtins()

    def _register_builtins(self):
        # Model lifecycle FSM
        model_fsm = FSMDefinition(
            "model_lifecycle",
            initial="unloaded",
            states={"unloaded", "loading", "ready", "busy", "error", "unloading"},
        )
        model_fsm.add_transition("unloaded", "load", "loading")
        model_fsm.add_transition("loading", "loaded", "ready")
        model_fsm.add_transition("loading", "error", "error")
        model_fsm.add_transition("ready", "request", "busy")
        model_fsm.add_transition("busy", "done", "ready")
        model_fsm.add_transition("busy", "error", "error")
        model_fsm.add_transition("error", "retry", "loading")
        model_fsm.add_transition("error", "reset", "unloaded")
        model_fsm.add_transition("ready", "unload", "unloading")
        model_fsm.add_transition("unloading", "unloaded", "unloaded")
        self._definitions["model_lifecycle"] = model_fsm

        # Request pipeline FSM
        pipeline_fsm = FSMDefinition(
            "request_pipeline",
            initial="received",
            states={
                "received",
                "validating",
                "routing",
                "processing",
                "responding",
                "done",
                "failed",
            },
            final_states={"done", "failed"},
        )
        pipeline_fsm.add_transition("received", "validate", "validating")
        pipeline_fsm.add_transition("validating", "valid", "routing")
        pipeline_fsm.add_transition("validating", "invalid", "failed")
        pipeline_fsm.add_transition("routing", "routed", "processing")
        pipeline_fsm.add_transition("routing", "no_route", "failed")
        pipeline_fsm.add_transition("processing", "complete", "responding")
        pipeline_fsm.add_transition("processing", "error", "failed")
        pipeline_fsm.add_transition("responding", "sent", "done")
        self._definitions["request_pipeline"] = pipeline_fsm

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register(self, definition: FSMDefinition):
        self._definitions[definition.name] = definition

    def create(
        self,
        definition_name: str,
        instance_id: str | None = None,
        context: dict | None = None,
    ) -> StateMachineInstance:
        defn = self._definitions.get(definition_name)
        if not defn:
            raise ValueError(f"FSM definition '{definition_name}' not found")
        iid = instance_id or f"{definition_name}-{int(time.time() * 1000) % 100000}"
        instance = StateMachineInstance(iid, defn, context)
        self._instances[iid] = instance
        return instance

    def get(self, instance_id: str) -> StateMachineInstance | None:
        return self._instances.get(instance_id)

    async def send(
        self, instance_id: str, event: str, data: dict | None = None
    ) -> tuple[bool, str]:
        instance = self._instances.get(instance_id)
        if not instance:
            return False, f"Instance '{instance_id}' not found"
        ok, result = await instance.send(event, data)
        if ok and self.redis:
            await self.redis.setex(
                f"{REDIS_PREFIX}{instance_id}",
                3600,
                json.dumps(instance.to_dict()),
            )
        return ok, result

    def list_instances(self, definition: str | None = None) -> list[dict]:
        instances = list(self._instances.values())
        if definition:
            instances = [i for i in instances if i.definition.name == definition]
        return [i.to_dict() for i in instances]

    def stats(self) -> dict:
        instances = list(self._instances.values())
        return {
            "definitions": list(self._definitions.keys()),
            "instances": len(instances),
            "by_state": {},
            "final": sum(1 for i in instances if i.is_final),
        }


async def main():
    import sys

    engine = StateMachineEngine()
    await engine.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Model lifecycle demo
        inst = engine.create("model_lifecycle", "qwen3.5-m1")
        print(f"Initial: {inst.current_state}")

        events = [
            "load",
            "loaded",
            "request",
            "done",
            "request",
            "error",
            "retry",
            "loaded",
        ]
        for event in events:
            ok, state = await engine.send(inst.instance_id, event)
            print(f"  send({event!r}) → {'✅' if ok else '❌'} {state}")

        print(f"\nAvailable events: {inst.available_events()}")
        print(f"History: {[h.event for h in inst.history]}")

        # Request pipeline demo
        print("\nRequest pipeline:")
        req = engine.create("request_pipeline", "req-001")
        for event in ["validate", "valid", "routed", "complete", "sent"]:
            ok, state = await engine.send(req.instance_id, event)
            print(f"  {event!r} → {state} {'(final)' if req.is_final else ''}")

    elif cmd == "stats":
        print(json.dumps(engine.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
