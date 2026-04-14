#!/usr/bin/env python3
"""MCP Server jarvis-memory — mémoire persistante unifiée Claude+OpenClaw."""
import asyncio, json
from pathlib import Path
from datetime import datetime
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server('jarvis-memory')
MEMORY_DIR = Path.home() / '.openclaw/memory'

@app.list_tools()
async def list_tools():
    return [
        Tool(name='save_memory', description='Save a memory entry',
             inputSchema={'type':'object','properties':{'type':{'type':'string','enum':['user','project','feedback','reference']},'name':{'type':'string'},'content':{'type':'string'}},'required':['type','name','content']}),
        Tool(name='list_memories', description='List all memory entries',
             inputSchema={'type':'object','properties':{}}),
        Tool(name='search_memory', description='Search memory by keyword',
             inputSchema={'type':'object','properties':{'query':{'type':'string'}},'required':['query']}),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == 'save_memory':
        t, n, c = arguments['type'], arguments['name'], arguments['content']
        path = MEMORY_DIR / t / f'{n}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nname: {n}\ntype: {t}\ndate: {datetime.now().isoformat()}\n---\n\n{c}\n')
        return [TextContent(type='text', text=f'Saved: {path}')]
    if name == 'list_memories':
        files = list(MEMORY_DIR.rglob('*.md'))
        return [TextContent(type='text', text='\n'.join(str(f.relative_to(MEMORY_DIR)) for f in files))]
    if name == 'search_memory':
        q = arguments['query'].lower()
        results = []
        for f in MEMORY_DIR.rglob('*.md'):
            content = f.read_text()
            if q in content.lower():
                results.append(f'{f.name}: {content[:200]}')
        return [TextContent(type='text', text='\n---\n'.join(results) or 'Aucun résultat')]

async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

if __name__ == '__main__':
    asyncio.run(main())
