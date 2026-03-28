"""JARVIS Service Registry — Registration, health checks, fallback routing."""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("jarvis.services")


@dataclass
class ServiceInfo:
    name: str
    host: str
    port: int
    type: str  # lmstudio, ollama, browseros, n8n, custom
    tags: List[str] = field(default_factory=list)
    healthy: bool = True
    fallback: Optional[str] = None  # name of fallback service


class ServiceRegistry:
    """Central registry for all JARVIS cluster services."""

    def __init__(self):
        self._services: Dict[str, ServiceInfo] = {}
        self._register_defaults()

    def _register_defaults(self):
        defaults = [
            ServiceInfo("M1", "127.0.0.1", 1234, "lmstudio",
                        ["gpu", "fast", "local"], fallback="M3"),
            ServiceInfo("M2", "192.168.1.26", 1234, "lmstudio",
                        ["gpu", "code", "remote"], fallback="M3"),
            ServiceInfo("M3", "192.168.1.113", 1234, "lmstudio",
                        ["gpu", "deep", "remote", "champion"], fallback="M1"),
            ServiceInfo("OL1", "127.0.0.1", 11434, "ollama",
                        ["gpu", "local", "lightweight"], fallback="M1"),
            ServiceInfo("BrowserOS", "127.0.0.1", 9222, "browseros",
                        ["browser", "automation"]),
            ServiceInfo("n8n", "127.0.0.1", 5678, "n8n",
                        ["workflow", "automation"]),
        ]
        for svc in defaults:
            self._services[svc.name] = svc

    def register(self, name: str, host: str, port: int, type: str,
                 tags: Optional[List[str]] = None, fallback: Optional[str] = None):
        self._services[name] = ServiceInfo(
            name=name, host=host, port=port, type=type,
            tags=tags or [], fallback=fallback,
        )

    def get(self, name: str) -> Optional[ServiceInfo]:
        return self._services.get(name)

    def get_by_tag(self, tag: str) -> List[ServiceInfo]:
        return [s for s in self._services.values() if tag in s.tags]

    def health_check(self, name: str) -> bool:
        """Ping a service and update its health status."""
        svc = self._services.get(name)
        if not svc:
            return False
        try:
            if svc.type == "ollama":
                url = f"http://{svc.host}:{svc.port}/api/tags"
            elif svc.type == "lmstudio":
                url = f"http://{svc.host}:{svc.port}/v1/models"
            elif svc.type == "n8n":
                url = f"http://{svc.host}:{svc.port}/healthz"
            else:
                url = f"http://{svc.host}:{svc.port}/"
            resp = requests.get(url, timeout=5)
            svc.healthy = resp.status_code < 500
        except Exception:
            svc.healthy = False
        logger.info("Health %s: %s", name, "OK" if svc.healthy else "DOWN")
        return svc.healthy

    def health_check_all(self) -> Dict[str, bool]:
        return {name: self.health_check(name) for name in self._services}

    def fallback(self, name: str) -> Optional[ServiceInfo]:
        """Return the next available fallback service."""
        svc = self._services.get(name)
        if not svc or not svc.fallback:
            return None
        fb = self._services.get(svc.fallback)
        if fb and (fb.healthy or self.health_check(fb.name)):
            return fb
        # Try fallback's fallback (one level deep)
        if fb and fb.fallback:
            fb2 = self._services.get(fb.fallback)
            if fb2 and (fb2.healthy or self.health_check(fb2.name)):
                return fb2
        return None

    def list_all(self) -> List[ServiceInfo]:
        return list(self._services.values())
