#!/usr/bin/env python3
"""
jarvis_access_control — Role-based access control (RBAC) for agents and APIs
Roles, permissions, policies, and resource-level access decisions
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.access_control")

REDIS_PREFIX = "jarvis:acl:"


class Effect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class ResourceKind(str, Enum):
    INFERENCE = "inference"
    TRADING = "trading"
    CLUSTER = "cluster"
    ADMIN = "admin"
    MONITORING = "monitoring"
    STORAGE = "storage"
    API = "api"
    ANY = "*"


class Action(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DELETE = "delete"
    ADMIN = "admin"
    ANY = "*"


@dataclass
class Permission:
    resource_kind: ResourceKind
    action: Action
    resource_id: str = "*"  # specific resource or wildcard
    conditions: dict[str, Any] = field(default_factory=dict)

    def matches(
        self, kind: ResourceKind, action: Action, resource_id: str = "*"
    ) -> bool:
        kind_match = self.resource_kind in (kind, ResourceKind.ANY)
        action_match = self.action in (action, Action.ANY)
        id_match = self.resource_id in ("*", resource_id)
        return kind_match and action_match and id_match

    def to_dict(self) -> dict:
        return {
            "resource": self.resource_kind.value,
            "action": self.action.value,
            "resource_id": self.resource_id,
        }


@dataclass
class Policy:
    policy_id: str
    name: str
    effect: Effect
    permissions: list[Permission] = field(default_factory=list)
    description: str = ""
    priority: int = 5  # lower = higher priority


@dataclass
class Role:
    role_id: str
    name: str
    policies: list[str] = field(default_factory=list)  # policy_ids
    description: str = ""
    parent_role: str = ""  # role inheritance


@dataclass
class Principal:
    principal_id: str
    kind: str = "agent"  # agent, user, service
    roles: list[str] = field(default_factory=list)
    direct_policies: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "principal_id": self.principal_id,
            "kind": self.kind,
            "roles": self.roles,
            "direct_policies": self.direct_policies,
            "active": self.active,
        }


@dataclass
class AccessDecision:
    allowed: bool
    principal_id: str
    resource_kind: str
    action: str
    resource_id: str
    reason: str = ""
    policy_id: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "principal_id": self.principal_id,
            "resource_kind": self.resource_kind,
            "action": self.action,
            "resource_id": self.resource_id,
            "reason": self.reason,
            "policy_id": self.policy_id,
        }


class AccessControl:
    def __init__(self, default_effect: Effect = Effect.DENY):
        self.redis: aioredis.Redis | None = None
        self._principals: dict[str, Principal] = {}
        self._roles: dict[str, Role] = {}
        self._policies: dict[str, Policy] = {}
        self._default_effect = default_effect
        self._decision_log: list[AccessDecision] = []
        self._max_log = 10_000
        self._stats: dict[str, int] = {
            "checks": 0,
            "allowed": 0,
            "denied": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def define_policy(self, policy: Policy):
        self._policies[policy.policy_id] = policy

    def define_role(self, role: Role):
        self._roles[role.role_id] = role

    def register_principal(self, principal: Principal):
        self._principals[principal.principal_id] = principal

    def assign_role(self, principal_id: str, role_id: str):
        p = self._principals.get(principal_id)
        if p and role_id not in p.roles:
            p.roles.append(role_id)

    def revoke_role(self, principal_id: str, role_id: str):
        p = self._principals.get(principal_id)
        if p:
            p.roles = [r for r in p.roles if r != role_id]

    def _get_all_policies(self, principal: Principal) -> list[Policy]:
        policy_ids: set[str] = set(principal.direct_policies)

        # Collect from roles (with inheritance)
        roles_to_check = list(principal.roles)
        visited: set[str] = set()
        while roles_to_check:
            rid = roles_to_check.pop()
            if rid in visited:
                continue
            visited.add(rid)
            role = self._roles.get(rid)
            if role:
                policy_ids.update(role.policies)
                if role.parent_role and role.parent_role not in visited:
                    roles_to_check.append(role.parent_role)

        policies = [self._policies[pid] for pid in policy_ids if pid in self._policies]
        return sorted(policies, key=lambda p: p.priority)

    def check(
        self,
        principal_id: str,
        resource_kind: ResourceKind,
        action: Action,
        resource_id: str = "*",
    ) -> AccessDecision:
        self._stats["checks"] += 1
        principal = self._principals.get(principal_id)

        if not principal or not principal.active:
            decision = AccessDecision(
                allowed=False,
                principal_id=principal_id,
                resource_kind=resource_kind.value,
                action=action.value,
                resource_id=resource_id,
                reason="principal not found or inactive",
            )
            self._stats["denied"] += 1
            self._log_decision(decision)
            return decision

        policies = self._get_all_policies(principal)

        # Evaluate policies in priority order
        # DENY policies take precedence over ALLOW at same priority
        for policy in policies:
            for perm in policy.permissions:
                if perm.matches(resource_kind, action, resource_id):
                    allowed = policy.effect == Effect.ALLOW
                    if not allowed:
                        # Explicit deny — short-circuit
                        decision = AccessDecision(
                            allowed=False,
                            principal_id=principal_id,
                            resource_kind=resource_kind.value,
                            action=action.value,
                            resource_id=resource_id,
                            reason=f"denied by policy {policy.policy_id!r}",
                            policy_id=policy.policy_id,
                        )
                        self._stats["denied"] += 1
                        self._log_decision(decision)
                        return decision
                    # Found allow — continue checking for any explicit deny
                    allow_policy = policy

        # Check if any allow was found
        for policy in policies:
            for perm in policy.permissions:
                if (
                    perm.matches(resource_kind, action, resource_id)
                    and policy.effect == Effect.ALLOW
                ):
                    decision = AccessDecision(
                        allowed=True,
                        principal_id=principal_id,
                        resource_kind=resource_kind.value,
                        action=action.value,
                        resource_id=resource_id,
                        reason=f"allowed by policy {policy.policy_id!r}",
                        policy_id=policy.policy_id,
                    )
                    self._stats["allowed"] += 1
                    self._log_decision(decision)
                    return decision

        # Default effect
        allowed = self._default_effect == Effect.ALLOW
        decision = AccessDecision(
            allowed=allowed,
            principal_id=principal_id,
            resource_kind=resource_kind.value,
            action=action.value,
            resource_id=resource_id,
            reason=f"default {self._default_effect.value}",
        )
        if allowed:
            self._stats["allowed"] += 1
        else:
            self._stats["denied"] += 1
        self._log_decision(decision)
        return decision

    def is_allowed(
        self,
        principal_id: str,
        resource_kind: ResourceKind,
        action: Action,
        resource_id: str = "*",
    ) -> bool:
        return self.check(principal_id, resource_kind, action, resource_id).allowed

    def _log_decision(self, decision: AccessDecision):
        self._decision_log.append(decision)
        if len(self._decision_log) > self._max_log:
            self._decision_log.pop(0)

    def recent_decisions(
        self, principal_id: str | None = None, limit: int = 20
    ) -> list[dict]:
        log_entries = self._decision_log
        if principal_id:
            log_entries = [d for d in log_entries if d.principal_id == principal_id]
        return [d.to_dict() for d in log_entries[-limit:]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "principals": len(self._principals),
            "roles": len(self._roles),
            "policies": len(self._policies),
            "deny_rate": round(
                self._stats["denied"] / max(self._stats["checks"], 1), 4
            ),
        }


def build_jarvis_access_control() -> AccessControl:
    ac = AccessControl(default_effect=Effect.DENY)

    # Policies
    ac.define_policy(
        Policy(
            "p-inference-rw",
            "Inference read/write",
            Effect.ALLOW,
            [
                Permission(ResourceKind.INFERENCE, Action.READ),
                Permission(ResourceKind.INFERENCE, Action.EXECUTE),
            ],
        )
    )
    ac.define_policy(
        Policy(
            "p-trading-rw",
            "Trading read/write",
            Effect.ALLOW,
            [
                Permission(ResourceKind.TRADING, Action.READ),
                Permission(ResourceKind.TRADING, Action.EXECUTE),
            ],
        )
    )
    ac.define_policy(
        Policy(
            "p-monitoring-r",
            "Monitoring read-only",
            Effect.ALLOW,
            [
                Permission(ResourceKind.MONITORING, Action.READ),
                Permission(ResourceKind.CLUSTER, Action.READ),
            ],
        )
    )
    ac.define_policy(
        Policy(
            "p-admin-all",
            "Admin full access",
            Effect.ALLOW,
            [
                Permission(ResourceKind.ANY, Action.ANY),
            ],
            priority=1,
        )
    )

    # Roles
    ac.define_role(
        Role("role-inference", "Inference Agent", ["p-inference-rw", "p-monitoring-r"])
    )
    ac.define_role(
        Role("role-trading", "Trading Agent", ["p-trading-rw", "p-monitoring-r"])
    )
    ac.define_role(Role("role-monitor", "Monitor Agent", ["p-monitoring-r"]))
    ac.define_role(
        Role(
            "role-admin", "Administrator", ["p-admin-all"], parent_role="role-inference"
        )
    )

    # Principals
    ac.register_principal(Principal("inference-gw", "agent", roles=["role-inference"]))
    ac.register_principal(Principal("trading-agent", "agent", roles=["role-trading"]))
    ac.register_principal(Principal("monitor-health", "agent", roles=["role-monitor"]))
    ac.register_principal(Principal("admin-cli", "user", roles=["role-admin"]))

    return ac


async def main():
    import sys

    ac = build_jarvis_access_control()
    await ac.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Access control demo...")
        checks = [
            ("inference-gw", ResourceKind.INFERENCE, Action.EXECUTE),
            ("inference-gw", ResourceKind.TRADING, Action.EXECUTE),
            ("trading-agent", ResourceKind.TRADING, Action.EXECUTE),
            ("trading-agent", ResourceKind.ADMIN, Action.ADMIN),
            ("monitor-health", ResourceKind.MONITORING, Action.READ),
            ("monitor-health", ResourceKind.INFERENCE, Action.WRITE),
            ("admin-cli", ResourceKind.ADMIN, Action.ADMIN),
            ("unknown-agent", ResourceKind.INFERENCE, Action.READ),
        ]
        for pid, kind, action in checks:
            d = ac.check(pid, kind, action)
            icon = "✅" if d.allowed else "❌"
            print(
                f"  {icon} {pid:<20} {kind.value:<14} {action.value:<10} → {d.reason}"
            )

        print(f"\nStats: {json.dumps(ac.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(ac.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
