"""SchemaInspector — compare real DB schema against expected definitions."""

import sqlite3
from pathlib import Path
from typing import Any


class SchemaInspector:
    """Inspect and diff SQLite schemas."""

    @staticmethod
    def inspect(db_path: str | Path) -> dict[str, list[dict]]:
        """Return {table_name: [{name, type, notnull, pk}, ...]} for every table."""
        conn = sqlite3.connect(str(db_path))
        tables: dict[str, list[dict]] = {}
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        for (table_name,) in cursor.fetchall():
            cols = conn.execute(f"PRAGMA table_info([{table_name}])").fetchall()
            tables[table_name] = [
                {"name": c[1], "type": c[2], "notnull": bool(c[3]), "pk": bool(c[5])}
                for c in cols
            ]
        conn.close()
        return tables

    @staticmethod
    def diff(db_path: str | Path, expected: dict[str, list[str]]) -> dict[str, Any]:
        """Compare real schema against expected {table: [col_names]}.

        Returns {missing_tables, missing_columns, extra_columns}.
        """
        real = SchemaInspector.inspect(db_path)
        real_tables = set(real.keys())
        expected_tables = set(expected.keys())

        missing_tables = sorted(expected_tables - real_tables)
        missing_columns: dict[str, list[str]] = {}
        extra_columns: dict[str, list[str]] = {}

        for table in expected_tables & real_tables:
            real_cols = {c["name"] for c in real[table]}
            expected_cols = set(expected[table])
            miss = sorted(expected_cols - real_cols)
            extra = sorted(real_cols - expected_cols)
            if miss:
                missing_columns[table] = miss
            if extra:
                extra_columns[table] = extra

        return {
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
            "extra_columns": extra_columns,
        }

    @staticmethod
    def report(db_path: str | Path) -> str:
        """Human-readable schema report."""
        tables = SchemaInspector.inspect(db_path)
        lines = [f"Schema report for {db_path}", f"Tables: {len(tables)}", ""]
        for table, cols in sorted(tables.items()):
            lines.append(f"  {table} ({len(cols)} columns)")
            for c in cols:
                pk = " PK" if c["pk"] else ""
                nn = " NOT NULL" if c["notnull"] else ""
                lines.append(f"    - {c['name']} {c['type']}{pk}{nn}")
        return "\n".join(lines)
