"""MemoryFacade — unified access to all JARVIS SQLite databases."""

import sqlite3
import json
import os
from pathlib import Path
from typing import Any


DB_DIR = Path(os.environ.get("JARVIS_DB_DIR", Path.home() / "IA" / "Core" / "jarvis" / "data"))

DEFAULT_DBS = {
    "master": DB_DIR / "jarvis-master.db",
    "etoile": DB_DIR / "etoile.db",
    "jarvis": DB_DIR / "jarvis.db",
}


class MemoryFacade:
    """Single entry point for all JARVIS database operations."""

    def __init__(self, db_map: dict[str, Path] | None = None, read_only: bool = False):
        self._db_map: dict[str, Path] = db_map or dict(DEFAULT_DBS)
        self._read_only = read_only
        self._connections: dict[str, sqlite3.Connection] = {}

    # -- connection pooling --------------------------------------------------

    def _conn(self, db_name: str) -> sqlite3.Connection:
        if db_name not in self._db_map:
            raise KeyError(f"Unknown database: {db_name}")
        if db_name not in self._connections:
            path = str(self._db_map[db_name])
            uri = f"file:{path}?mode=ro" if self._read_only else path
            # Optimized connection with timeout for concurrent access
            conn = sqlite3.connect(uri, uri=self._read_only, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            
            # High-performance tuning (WAL mode + Normal sync)
            if not self._read_only:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute("PRAGMA cache_size = -2000")  # 2MB cache
                
            self._connections[db_name] = conn
        return self._connections[db_name]

    def close_all(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()

    # -- core operations -----------------------------------------------------

    def query(self, db_name: str, sql: str, params: tuple = ()) -> list[dict]:
        cur = self._conn(db_name).execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def insert(self, db_name: str, table: str, data: dict[str, Any]) -> int:
        if self._read_only:
            raise PermissionError("Facade is in read-only mode")
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        conn = self._conn(db_name)
        cur = conn.execute(sql, tuple(data.values()))
        conn.commit()
        return cur.lastrowid

    # -- introspection -------------------------------------------------------

    def _tables(self, db_name: str) -> list[str]:
        rows = self.query(db_name, "SELECT name FROM sqlite_master WHERE type='table'")
        return [r["name"] for r in rows]

    def get_stats(self) -> dict:
        stats: dict[str, dict] = {}
        for name, path in self._db_map.items():
            try:
                tables = self._tables(name)
                row_counts = {}
                for t in tables:
                    cnt = self.query(name, f"SELECT COUNT(*) AS c FROM [{t}]")
                    row_counts[t] = cnt[0]["c"] if cnt else 0
                stats[name] = {
                    "tables": len(tables),
                    "rows": sum(row_counts.values()),
                    "size_kb": round(path.stat().st_size / 1024, 1) if path.exists() else 0,
                    "detail": row_counts,
                }
            except Exception as exc:
                stats[name] = {"error": str(exc)}
        return stats

    def health_check(self) -> dict:
        result: dict[str, dict] = {}
        for name, path in self._db_map.items():
            try:
                self.query(name, "SELECT 1")
                result[name] = {"ok": True, "path": str(path)}
            except Exception as exc:
                result[name] = {"ok": False, "error": str(exc)}
        return result

    def export_json(self, db_name: str) -> dict:
        dump: dict[str, list[dict]] = {}
        for table in self._tables(db_name):
            dump[table] = self.query(db_name, f"SELECT * FROM [{table}]")
        return dump

    # -- register extra databases --------------------------------------------

    def register(self, name: str, path: Path) -> None:
        self._db_map[name] = path

    def __repr__(self) -> str:
        return f"<MemoryFacade dbs={list(self._db_map.keys())} ro={self._read_only}>"
