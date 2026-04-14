#!/usr/bin/env python3
"""JARVIS Dependency Graph — Map inter-module dependencies + detect cycles"""
import ast, os, redis, json
from pathlib import Path

r = redis.Redis(decode_responses=True)
CORE_DIR = Path("/home/turbo/jarvis/core")

def extract_imports(filepath: str) -> list:
    try:
        with open(filepath) as f:
            tree = ast.parse(f.read())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        return [i for i in imports if i.startswith("jarvis")]
    except:
        return []

def build_graph() -> dict:
    graph = {}
    for pyfile in CORE_DIR.glob("jarvis_*.py"):
        name = pyfile.stem
        deps = extract_imports(str(pyfile))
        if deps:
            graph[name] = deps
    return graph

def find_cycles(graph: dict) -> list:
    visited, rec_stack, cycles = set(), set(), []
    def dfs(node, path):
        visited.add(node); rec_stack.add(node)
        for dep in graph.get(node, []):
            if dep not in visited:
                dfs(dep, path + [dep])
            elif dep in rec_stack:
                cycles.append(path + [dep])
    for node in graph:
        if node not in visited:
            dfs(node, [node])
    return cycles

def run():
    graph = build_graph()
    cycles = find_cycles(graph)
    result = {"modules": len(graph), "deps": graph, "cycles": cycles}
    r.setex("jarvis:dep_graph", 3600, json.dumps(result))
    return result

if __name__ == "__main__":
    res = run()
    print(f"Modules: {res['modules']} | Cycles: {len(res['cycles'])}")
    for mod, deps in res["deps"].items():
        print(f"  {mod} → {deps}")
