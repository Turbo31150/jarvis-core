"""SQLOperator — read-only SQL agent backed by MemoryFacade."""

import json
from datetime import datetime
from pathlib import Path

from core.memory.facade import MemoryFacade


class SQLOperator:
    """Agent for safe, read-only database queries across JARVIS DBs."""

    def __init__(self, read_only: bool = True):
        self.mem = MemoryFacade(read_only=read_only)

    def get_stats(self) -> dict:
        """Return size / table / row statistics for every registered DB."""
        return {
            "timestamp": datetime.now().isoformat(),
            "databases": self.mem.get_stats(),
        }

    def query(self, db: str, sql: str, params: tuple = ()) -> dict:
        """Execute an arbitrary SQL query (read-only by default)."""
        try:
            rows = self.mem.query(db, sql, params)
            return {
                "db": db,
                "sql": sql,
                "rows": rows,
                "count": len(rows),
            }
        except Exception as exc:
            return {"db": db, "sql": sql, "error": str(exc)}

    def health_check(self) -> dict:
        """Check connectivity to every registered database."""
        return {
            "timestamp": datetime.now().isoformat(),
            "health": self.mem.health_check(),
        }

    def export_table(self, db: str, table: str) -> dict:
        """Export all rows of a single table as a list of dicts."""
        try:
            rows = self.mem.query(db, f"SELECT * FROM [{table}]")
            return {"db": db, "table": table, "rows": rows, "count": len(rows)}
        except Exception as exc:
            return {"db": db, "table": table, "error": str(exc)}

    def list_tables(self, db: str) -> dict:
        """List all tables in the given database."""
        try:
            tables = self.mem.query(db, "SELECT name FROM sqlite_master WHERE type='table'")
            names = [r["name"] for r in tables]
            return {"db": db, "tables": names, "count": len(names)}
        except Exception as exc:
            return {"db": db, "error": str(exc)}

    def row_counts(self) -> dict:
        """Return row counts per table across all databases."""
        stats = self.mem.get_stats()
        counts: dict[str, dict[str, int]] = {}
        for db_name, info in stats.items():
            if "detail" in info:
                counts[db_name] = info["detail"]
            elif "error" in info:
                counts[db_name] = {"_error": info["error"]}
        return {"timestamp": datetime.now().isoformat(), "row_counts": counts}

    def close(self) -> None:
        self.mem.close_all()


if __name__ == "__main__":
    op = SQLOperator(read_only=True)
    print(json.dumps(op.health_check(), indent=2, default=str))
    print(json.dumps(op.get_stats(), indent=2, default=str))
    op.close()
