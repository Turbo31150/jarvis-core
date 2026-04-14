#!/usr/bin/env python3
"""MCP Server jarvis-agents — invoquer les agents OpenClaw depuis Claude Code."""
import asyncio, subprocess, json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server('jarvis-agents')

AGENTS = ['main','master','sys-ops','linux-admin','ai-engine','cluster-mgr','trading-engine',
          'automation','monitoring','ops-sre','voice-engine','comms','browser-ops',
          'linkedin-agent','codeur-agent','mail-agent','cowork-codegen','cowork-testing',
          'omega-analysis-agent','omega-dev-agent','omega-docs-agent','omega-security-agent',
          'omega-system-agent','omega-trading-agent','jarvis-cron-agent','github-agent']

@app.list_tools()
async def list_tools():
    return [
        Tool(name='invoke_agent', description='Invoke an OpenClaw agent with a message',
             inputSchema={'type':'object','properties':{'agent_id':{'type':'string','enum':AGENTS},'message':{'type':'string'},'deliver':{'type':'boolean','default':True}},'required':['agent_id','message']}),
        Tool(name='list_agents', description='List all available OpenClaw agents',
             inputSchema={'type':'object','properties':{}}),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == 'list_agents':
        return [TextContent(type='text', text=json.dumps(AGENTS))]
    if name == 'invoke_agent':
        cmd = ['openclaw','agent','--agent',arguments['agent_id'],'--message',arguments['message']]
        if arguments.get('deliver', True):
            cmd += ['--deliver','--channel','telegram']
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return [TextContent(type='text', text=r.stdout or r.stderr or 'Dispatched')]

async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

if __name__ == '__main__':
    asyncio.run(main())
