"""JARVIS Capability Registry — Agent capability matching and routing."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AgentCapability:
    agent_name: str
    capabilities: List[str]
    tags: List[str] = field(default_factory=list)
    priority: int = 5  # 1=highest, 10=lowest


class CapabilityRegistry:
    """Registry mapping capabilities to agents for smart routing."""

    def __init__(self):
        self._agents: Dict[str, AgentCapability] = {}
        self._pre_register_defaults()

    def register(self, agent_name: str, capabilities: List[str],
                 tags: Optional[List[str]] = None, priority: int = 5):
        """Register an agent with its capabilities and tags."""
        self._agents[agent_name] = AgentCapability(
            agent_name=agent_name,
            capabilities=capabilities,
            tags=tags or [],
            priority=priority,
        )

    def find(self, capability: str) -> List[str]:
        """Find all agents that declare a given capability."""
        return [
            a.agent_name for a in self._agents.values()
            if capability in a.capabilities
        ]

    def find_best(self, capability: str) -> Optional[str]:
        """Find the best agent for a capability (lowest priority number)."""
        candidates = [
            a for a in self._agents.values()
            if capability in a.capabilities
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda a: a.priority)
        return candidates[0].agent_name

    def list_all(self) -> Dict[str, Dict]:
        """Return all agents and their capabilities."""
        return {
            name: {
                "capabilities": a.capabilities,
                "tags": a.tags,
                "priority": a.priority,
            }
            for name, a in self._agents.items()
        }

    def _pre_register_defaults(self):
        defaults = [
            ("github_operator", ["github", "git", "pr", "issues", "repos"],
             ["vcs", "code"], 2),
            ("browseros_operator", ["browse", "scrape", "screenshot", "web_automation"],
             ["browser", "web"], 2),
            ("telegram_operator", ["telegram", "notify", "message", "bot"],
             ["messaging", "alerts"], 3),
            ("network_operator", ["ping", "http_check", "dns", "port_scan"],
             ["network", "infra"], 3),
            ("sql_operator", ["query", "sql", "database", "migrate"],
             ["data", "storage"], 2),
            ("voice_router", ["tts", "stt", "voice_command", "easyspeech"],
             ["voice", "audio"], 4),
            ("container_operator", ["docker", "container", "compose", "image"],
             ["infra", "devops"], 3),
            ("mail_operator", ["email", "smtp", "inbox", "send_mail"],
             ["messaging", "email"], 4),
        ]
        for name, caps, tags, prio in defaults:
            self.register(name, caps, tags, prio)


registry = CapabilityRegistry()

if __name__ == "__main__":
    r = CapabilityRegistry()

    print("=== All Agents ===")
    for name, info in r.list_all().items():
        print(f"  {name}: {info['capabilities']} (priority={info['priority']})")

    print("\n=== Find 'github' ===")
    print(f"  agents: {r.find('github')}")
    print(f"  best:   {r.find_best('github')}")

    print("\n=== Find 'docker' ===")
    print(f"  agents: {r.find('docker')}")
    print(f"  best:   {r.find_best('docker')}")

    print("\n=== Find 'unknown_cap' ===")
    print(f"  agents: {r.find('unknown_cap')}")
    print(f"  best:   {r.find_best('unknown_cap')}")
