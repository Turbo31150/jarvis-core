#!/usr/bin/env python3
"""JARVIS Dependency Graph — Map module dependencies and detect circular refs"""

import os
import re
import json
from pathlib import Path
from datetime import datetime

CORE_DIR = Path(__file__).parent


def extract_imports(filepath: str) -> list:
    """Extract jarvis_* imports from a Python file"""
    imports = []
    try:
        with open(filepath) as f:
            content = f.read()
        for pattern in [
            r'from (jarvis_\w+) import',
            r'import (jarvis_\w+)',
            r'importlib\.import_module\("(jarvis_\w+)"\)',
        ]:
            imports.extend(re.findall(pattern, content))
    except Exception:
        pass
    return list(set(imports))


def build_graph() -> dict:
    """Build full dependency graph for all jarvis_*.py modules"""
    graph = {}
    for filepath in sorted(CORE_DIR.glob("jarvis_*.py")):
        module = filepath.stem
        deps = extract_imports(str(filepath))
        graph[module] = deps
    return graph


def find_cycles(graph: dict) -> list:
    """DFS cycle detection"""
    visited = set()
    rec_stack = set()
    cycles = []

    def dfs(node, path):
        visited.add(node)
        rec_stack.add(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if dep not in visited:
                dfs(dep, path + [dep])
            elif dep in rec_stack:
                cycle_start = path.index(dep) if dep in path else 0
                cycle = path[cycle_start:] + [dep]
                if cycle not in cycles:
                    cycles.append(cycle)
        rec_stack.discard(node)

    for node in graph:
        if node not in visited:
            dfs(node, [node])
    return cycles


def most_depended_on(graph: dict) -> list:
    """Modules depended on by the most others"""
    dep_count = {}
    for deps in graph.values():
        for d in deps:
            dep_count[d] = dep_count.get(d, 0) + 1
    return sorted(dep_count.items(), key=lambda x: -x[1])[:10]


def analyze() -> dict:
    graph = build_graph()
    cycles = find_cycles(graph)
    top_deps = most_depended_on(graph)
    # Modules with no dependencies (leaves)
    leaves = [m for m, deps in graph.items() if not deps]
    # Modules with most dependencies
    most_deps = sorted(graph.items(), key=lambda x: -len(x[1]))[:5]

    result = {
        "ts": datetime.now().isoformat()[:19],
        "total_modules": len(graph),
        "total_edges": sum(len(deps) for deps in graph.values()),
        "cycles": cycles,
        "cycle_count": len(cycles),
        "most_depended_on": top_deps,
        "most_dependencies": [(m, len(d)) for m, d in most_deps],
        "leaf_modules": len(leaves),
    }
    return result


if __name__ == "__main__":
    res = analyze()
    print(f"Modules: {res['total_modules']}, Edges: {res['total_edges']}")
    print(f"Cycles: {res['cycle_count']}")
    if res["cycles"]:
        for c in res["cycles"]:
            print(f"  ⚠️  {' → '.join(c)}")
    print("Most depended on:")
    for m, cnt in res["most_depended_on"][:5]:
        print(f"  {m}: {cnt} dependents")
    print("Most dependencies:")
    for m, cnt in res["most_dependencies"]:
        print(f"  {m}: {cnt} deps")
