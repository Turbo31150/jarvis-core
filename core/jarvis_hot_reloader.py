#!/usr/bin/env python3
"""
jarvis_hot_reloader — Module hot-reload with dependency tracking
Watches Python files for changes and reloads them without restart
"""

import asyncio
import importlib
import importlib.util
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("jarvis.hot_reloader")


@dataclass
class ModuleRecord:
    name: str
    path: str
    mtime: float
    reload_count: int = 0
    last_reload: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "mtime": self.mtime,
            "reload_count": self.reload_count,
            "last_reload": self.last_reload,
            "error": self.error,
        }


class HotReloader:
    def __init__(self, poll_interval_s: float = 1.0):
        self._watched: dict[str, ModuleRecord] = {}  # module_name → record
        self._callbacks: list[Callable[[str, Any], None]] = []
        self._poll_interval = poll_interval_s
        self._running = False
        self._task: asyncio.Task | None = None
        self._stats = {"total_reloads": 0, "errors": 0, "watches": 0}

    def watch(self, module_name: str, path: str | None = None) -> bool:
        """Watch a module by name. Auto-discovers path if not provided."""
        if module_name in self._watched:
            return True

        # Resolve path
        if path is None:
            mod = sys.modules.get(module_name)
            if mod and hasattr(mod, "__file__") and mod.__file__:
                path = mod.__file__
            else:
                # Try to find in current dir
                candidate = Path(f"{module_name.replace('.', '/')}.py")
                if candidate.exists():
                    path = str(candidate)

        if not path or not Path(path).exists():
            log.warning(f"Cannot watch '{module_name}': path not found")
            return False

        mtime = os.path.getmtime(path)
        self._watched[module_name] = ModuleRecord(
            name=module_name, path=path, mtime=mtime
        )
        self._stats["watches"] += 1
        log.debug(f"Watching: {module_name} @ {path}")
        return True

    def watch_directory(
        self, directory: str, pattern: str = "*.py", recursive: bool = False
    ):
        """Watch all Python files in a directory."""
        base = Path(directory)
        glob_fn = base.rglob if recursive else base.glob
        for pyfile in glob_fn(pattern):
            stem = pyfile.stem
            self.watch(stem, str(pyfile))

    def unwatch(self, module_name: str):
        self._watched.pop(module_name, None)

    def on_reload(self, callback: Callable[[str, Any], None]):
        """Register callback(module_name, module_object) called after each reload."""
        self._callbacks.append(callback)

    def _check_and_reload(self, record: ModuleRecord) -> bool:
        """Returns True if module was reloaded."""
        try:
            current_mtime = os.path.getmtime(record.path)
        except OSError:
            return False

        if current_mtime <= record.mtime:
            return False

        # File changed — reload
        log.info(f"Reloading: {record.name}")
        record.mtime = current_mtime

        try:
            if record.name in sys.modules:
                mod = importlib.reload(sys.modules[record.name])
            else:
                spec = importlib.util.spec_from_file_location(record.name, record.path)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    sys.modules[record.name] = mod
                else:
                    raise ImportError(f"Cannot load spec for {record.name}")

            record.reload_count += 1
            record.last_reload = time.time()
            record.error = ""
            self._stats["total_reloads"] += 1

            for cb in self._callbacks:
                try:
                    cb(record.name, mod)
                except Exception as e:
                    log.warning(f"Reload callback error: {e}")

            return True

        except Exception as e:
            record.error = str(e)[:200]
            self._stats["errors"] += 1
            log.error(f"Reload failed for {record.name}: {e}")
            return False

    async def _poll_loop(self):
        while self._running:
            for record in list(self._watched.values()):
                self._check_and_reload(record)
            await asyncio.sleep(self._poll_interval)

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info(
            f"HotReloader started (poll={self._poll_interval}s, watching={len(self._watched)})"
        )

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("HotReloader stopped")

    def force_reload(self, module_name: str) -> bool:
        record = self._watched.get(module_name)
        if not record:
            return False
        # Force by resetting mtime to 0
        saved = record.mtime
        record.mtime = 0.0
        result = self._check_and_reload(record)
        if not result:
            record.mtime = saved
        return result

    def list_watched(self) -> list[dict]:
        return [r.to_dict() for r in self._watched.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "watched_modules": len(self._watched),
            "modules_with_errors": sum(1 for r in self._watched.values() if r.error),
            "running": self._running,
        }


async def main():
    import sys

    reloader = HotReloader(poll_interval_s=0.5)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Create a temp module to watch
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write("VALUE = 1\n")
            tmppath = f.name

        modname = "jarvis_hot_test_module"
        reloader.watch(modname, tmppath)

        reloads = []
        reloader.on_reload(
            lambda name, mod: reloads.append((name, getattr(mod, "VALUE", None)))
        )

        await reloader.start()

        # Simulate a file change after 0.3s
        await asyncio.sleep(0.3)
        with open(tmppath, "w") as f:
            f.write("VALUE = 42\n")
        await asyncio.sleep(0.8)

        await reloader.stop()
        os.unlink(tmppath)

        print(f"Reload events: {reloads}")
        print(f"Stats: {json.dumps(reloader.stats(), indent=2)}")

    elif cmd == "watch" and len(sys.argv) > 2:
        directory = sys.argv[2]
        reloader.watch_directory(directory)
        reloader.on_reload(lambda name, _: print(f"Reloaded: {name}"))
        await reloader.start()
        print(f"Watching {directory} — Ctrl+C to stop")
        try:
            await asyncio.sleep(3600)
        except KeyboardInterrupt:
            pass
        await reloader.stop()


if __name__ == "__main__":
    asyncio.run(main())
