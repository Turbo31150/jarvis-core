#!/usr/bin/env python3
"""
jarvis_index_builder — Build searchable indexes from JARVIS data
Indexes modules, logs, configs, embeddings for fast retrieval
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.index_builder")

CORE_DIR = Path("/home/turbo/IA/Core/jarvis/core")
DATA_DIR = Path("/home/turbo/IA/Core/jarvis/data")
INDEX_FILE = Path("/home/turbo/IA/Core/jarvis/data/module_index.json")
REDIS_KEY = "jarvis:module_index"


@dataclass
class ModuleEntry:
    name: str
    path: str
    description: str = ""
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    cli_commands: list[str] = field(default_factory=list)
    redis_keys: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    size_lines: int = 0
    last_modified: float = 0.0

    def matches(self, query: str) -> float:
        """Relevance score for query (0.0-1.0)."""
        q = query.lower()
        score = 0.0
        if q in self.name.lower():
            score += 1.0
        if q in self.description.lower():
            score += 0.7
        if any(q in c.lower() for c in self.classes):
            score += 0.5
        if any(q in f.lower() for f in self.functions):
            score += 0.4
        if any(q in t.lower() for t in self.tags):
            score += 0.6
        return min(1.0, score)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "description": self.description,
            "classes": self.classes,
            "functions": self.functions[:10],
            "cli_commands": self.cli_commands,
            "redis_keys": self.redis_keys,
            "tags": self.tags,
            "size_lines": self.size_lines,
            "last_modified": self.last_modified,
        }


def _parse_module(path: Path) -> ModuleEntry:
    """Extract metadata from a Python module file."""
    entry = ModuleEntry(
        name=path.stem,
        path=str(path),
        last_modified=path.stat().st_mtime,
    )

    try:
        text = path.read_text(errors="replace")
        lines = text.split("\n")
        entry.size_lines = len(lines)

        # Description from docstring
        doc_match = re.search(r'"""(.*?)"""', text, re.DOTALL)
        if doc_match:
            first_line = doc_match.group(1).strip().split("\n")[0].strip()
            # Strip leading marker like "jarvis_xxx — "
            first_line = re.sub(r"^jarvis_\w+\s+[—-]\s+", "", first_line)
            entry.description = first_line[:120]

        # Classes
        entry.classes = re.findall(r"^class\s+(\w+)", text, re.MULTILINE)

        # Functions (top-level)
        entry.functions = re.findall(r"^(?:async\s+)?def\s+(\w+)", text, re.MULTILINE)
        entry.functions = [f for f in entry.functions if not f.startswith("_")][:15]

        # CLI commands from argparse/sys.argv patterns
        cli_matches = re.findall(
            r'"(\w+(?:-\w+)*)"\s*(?:in\s+sys\.argv|==\s*cmd)', text
        )
        entry.cli_commands = list(set(cli_matches))[:8]

        # Redis keys
        redis_matches = re.findall(r'"(jarvis:[a-zA-Z:_]+)"', text)
        entry.redis_keys = list(set(redis_matches))[:5]

        # Tags from name and description
        name_parts = entry.name.replace("jarvis_", "").split("_")
        entry.tags = name_parts + [
            w.lower()
            for w in entry.description.split()[:5]
            if len(w) > 4 and w.isalpha()
        ]

    except Exception as e:
        log.debug(f"Parse error {path.name}: {e}")

    return entry


class IndexBuilder:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._index: dict[str, ModuleEntry] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def build(self, directory: Path = CORE_DIR) -> int:
        """Scan directory and build module index."""
        count = 0
        for path in sorted(directory.glob("jarvis_*.py")):
            entry = _parse_module(path)
            self._index[entry.name] = entry
            count += 1
        log.info(f"Indexed {count} modules from {directory}")
        return count

    def save(self):
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(
            json.dumps(
                {
                    "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "count": len(self._index),
                    "modules": {k: v.to_dict() for k, v in self._index.items()},
                },
                indent=2,
            )
        )

    def load(self) -> bool:
        if INDEX_FILE.exists():
            try:
                data = json.loads(INDEX_FILE.read_text())
                for name, d in data.get("modules", {}).items():
                    e = ModuleEntry(
                        name=d["name"],
                        path=d["path"],
                        description=d.get("description", ""),
                        classes=d.get("classes", []),
                        functions=d.get("functions", []),
                        cli_commands=d.get("cli_commands", []),
                        redis_keys=d.get("redis_keys", []),
                        tags=d.get("tags", []),
                        size_lines=d.get("size_lines", 0),
                        last_modified=d.get("last_modified", 0),
                    )
                    self._index[name] = e
                return True
            except Exception:
                return False
        return False

    async def sync_redis(self):
        if not self.redis:
            return
        summary = {
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "count": len(self._index),
            "names": list(self._index.keys()),
        }
        await self.redis.set(REDIS_KEY, json.dumps(summary), ex=3600)

    def search(self, query: str, top_k: int = 10) -> list[tuple[float, ModuleEntry]]:
        scored = [(entry.matches(query), entry) for entry in self._index.values()]
        return sorted(
            [(s, e) for s, e in scored if s > 0],
            key=lambda x: -x[0],
        )[:top_k]

    def get(self, name: str) -> ModuleEntry | None:
        return self._index.get(name) or self._index.get(f"jarvis_{name}")

    def stats(self) -> dict:
        total_lines = sum(e.size_lines for e in self._index.values())
        all_tags: dict[str, int] = {}
        for e in self._index.values():
            for t in e.tags:
                all_tags[t] = all_tags.get(t, 0) + 1
        top_tags = sorted(all_tags.items(), key=lambda x: -x[1])[:10]
        return {
            "total_modules": len(self._index),
            "total_lines": total_lines,
            "avg_lines": round(total_lines / max(len(self._index), 1)),
            "top_tags": [t for t, _ in top_tags],
        }


async def main():
    import sys

    builder = IndexBuilder()
    await builder.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"

    if cmd == "build":
        n = builder.build()
        builder.save()
        await builder.sync_redis()
        s = builder.stats()
        print(
            f"Built index: {n} modules | {s['total_lines']:,} lines | avg {s['avg_lines']} lines/module"
        )
        print(f"Top tags: {', '.join(s['top_tags'])}")

    elif cmd == "search" and len(sys.argv) > 2:
        builder.load() or builder.build()
        query = " ".join(sys.argv[2:])
        results = builder.search(query)
        if not results:
            print(f"No results for '{query}'")
        else:
            print(f"Results for '{query}':")
            for score, entry in results:
                print(f"  {score:.2f} {entry.name:<40} {entry.description[:60]}")

    elif cmd == "get" and len(sys.argv) > 2:
        builder.load() or builder.build()
        entry = builder.get(sys.argv[2])
        if entry:
            print(json.dumps(entry.to_dict(), indent=2))
        else:
            print(f"Module '{sys.argv[2]}' not found")

    elif cmd == "stats":
        builder.load() or builder.build()
        s = builder.stats()
        print(json.dumps(s, indent=2))

    elif cmd == "list":
        builder.load() or builder.build()
        for name, e in sorted(builder._index.items()):
            print(f"  {name:<40} {e.size_lines:>5} lines  {e.description[:60]}")


if __name__ == "__main__":
    asyncio.run(main())
