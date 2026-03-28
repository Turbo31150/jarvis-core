"""JARVIS Unified Config Loader — .env, JSON, os.environ, singleton."""
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

_BASE = Path(__file__).resolve().parent.parent  # jarvis/


def _parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a .env file into a dict, ignoring comments and blanks."""
    env = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


class UnifiedConfigLoader:
    """Singleton config aggregating .env, config/*.json, and os.environ."""

    _instance: Optional["UnifiedConfigLoader"] = None

    def __new__(cls) -> "UnifiedConfigLoader":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self):
        if self._loaded:
            return
        self._data: Dict[str, Any] = {}
        self._json_configs: Dict[str, Any] = {}
        self._load_all()
        self._loaded = True

    def _load_all(self):
        # 1) .env at project root
        self._data.update(_parse_env_file(_BASE / ".env"))
        # 2) All JSON in config/
        config_dir = _BASE / "config"
        if config_dir.is_dir():
            for jf in sorted(config_dir.glob("*.json")):
                try:
                    payload = json.loads(jf.read_text(encoding="utf-8"))
                    self._json_configs[jf.stem] = payload
                except (json.JSONDecodeError, OSError):
                    pass
        # 3) os.environ (highest priority)
        self._data.update(os.environ)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by key (env-style)."""
        return self._data.get(key, default)

    def get_json(self, name: str) -> Optional[Dict]:
        """Get a loaded JSON config by filename stem."""
        return self._json_configs.get(name)

    def get_service(self, name: str) -> Dict[str, Any]:
        """Return {host, port, model} for a known cluster service."""
        services = {
            "m1": {"host": self.get("LM_STUDIO_1_URL", "http://127.0.0.1:1234"),
                    "port": 1234, "model": "gemma-3-4b"},
            "m2": {"host": self.get("LM_STUDIO_2_URL", "http://192.168.1.26:1234"),
                    "port": 1234, "model": "deepseek-coder"},
            "m3": {"host": self.get("LM_STUDIO_3_URL", "http://192.168.1.113:1234"),
                    "port": 1234, "model": "deepseek-r1-qwen3-8b"},
            "ol1": {"host": self.get("OLLAMA_URL", "http://127.0.0.1:11434"),
                     "port": 11434, "model": "qwen2.5:1.5b"},
        }
        return services.get(name, {"host": "unknown", "port": 0, "model": "none"})

    def get_cluster(self) -> Dict[str, Dict[str, Any]]:
        """Return all cluster node configs."""
        return {n: self.get_service(n) for n in ("m1", "m2", "m3", "ol1")}

    def reload(self):
        """Force reload all config sources."""
        self._data.clear()
        self._json_configs.clear()
        self._load_all()


config = UnifiedConfigLoader()

if __name__ == "__main__":
    c = UnifiedConfigLoader()
    print("=== Cluster Config ===")
    for node, info in c.get_cluster().items():
        print(f"  {node}: {info}")
    print(f"\nTELEGRAM_TOKEN present: {bool(c.get('TELEGRAM_TOKEN'))}")
    print(f"JSON configs loaded: {list(c._json_configs.keys())}")
    print(f"M3 service: {c.get_service('m3')}")
