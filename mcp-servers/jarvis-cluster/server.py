#!/usr/bin/env python3
"""MCP Server jarvis-cluster — health check + query modèles cluster."""
import asyncio, requests, json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server('jarvis-cluster')

@app.list_tools()
async def list_tools():
    return [
        Tool(name='health_check_all', description='Ping M1/M2/OL1, retourne statuts JSON', inputSchema={'type':'object','properties':{}}),
        Tool(name='list_models', description='Liste tous les modèles disponibles sur le cluster', inputSchema={'type':'object','properties':{}}),
        Tool(name='query_model', description='Interroge un modèle du cluster',
             inputSchema={'type':'object','properties':{'model':{'type':'string'},'prompt':{'type':'string'},'node':{'type':'string','default':'M1'}},'required':['prompt']}),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == 'health_check_all':
        results = {}
        for n, url in [('M1','http://192.168.1.85:1234'),('M2','http://192.168.1.26:1234'),('OL1','http://127.0.0.1:11434')]:
            try:
                r = requests.get(f'{url}/v1/models', timeout=3)
                results[n] = {'status':'OK','models': len(r.json().get('data',r.json().get('models',[]))) }
            except Exception as e:
                results[n] = {'status':'DOWN','error':str(e)}
        return [TextContent(type='text', text=json.dumps(results, indent=2))]
    if name == 'list_models':
        all_models = {}
        for n, url in [('M1','http://192.168.1.85:1234'),('M2','http://192.168.1.26:1234'),('OL1','http://127.0.0.1:11434')]:
            try:
                r = requests.get(f'{url}/v1/models', timeout=3)
                data = r.json()
                all_models[n] = [m.get('id','?') for m in data.get('data', data.get('models',[]))]
            except:
                all_models[n] = ['DOWN']
        return [TextContent(type='text', text=json.dumps(all_models, indent=2))]
    if name == 'query_model':
        node_urls = {'M1':'http://192.168.1.85:1234','M2':'http://192.168.1.26:1234','OL1':'http://127.0.0.1:11434'}
        url = node_urls.get(arguments.get('node','M1'),'http://192.168.1.85:1234')
        model = arguments.get('model','qwen3.5-9b')
        prompt = arguments['prompt']
        r = requests.post(f'{url}/v1/chat/completions',
            json={'model':model,'messages':[{'role':'user','content':prompt}],'max_tokens':500}, timeout=30)
        resp = r.json()['choices'][0]['message']['content']
        return [TextContent(type='text', text=resp)]

async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

if __name__ == '__main__':
    asyncio.run(main())
