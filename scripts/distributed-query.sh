#!/bin/bash
# JARVIS Distributed Multi-AI Query
# Usage: ./distributed-query.sh "Your question here"
# Sends to M2+M3 (cluster) + ChatGPT+Perplexity (BrowserOS)

CLI=~/.browseros/bin/browseros-cli
QUESTION="$1"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR=~/IA/Core/jarvis/data/workflow-runs
mkdir -p "$OUTPUT_DIR"

echo "🛰️ Distributed Query: $QUESTION"
echo ""

# 1. Cluster M2+M3 (parallel)
echo "📡 Cluster M2+M3..."
python3 -c "
import json,time,requests,re,concurrent.futures
Q='$QUESTION'
ANTI_THINK='You must respond directly without using <think> tags. Do not wrap your response in <think> tags. Give your answer immediately.'
def clean(c):
    c=re.sub(r'<think>.*?</think>','',c,flags=re.DOTALL).strip()
    c=re.sub(r'<think>.*','',c,flags=re.DOTALL).strip()
    return c
def q(n,u,m):
    try:
        is_ds='deepseek' in m.lower() and 'r1' in m.lower()
        msgs=[]
        if is_ds: msgs.append({'role':'system','content':ANTI_THINK})
        msgs.append({'role':'user','content':Q})
        payload={'model':m,'messages':msgs,'temperature':0.3,'max_tokens':800 if is_ds else 500}
        if is_ds: payload['chat_template_kwargs']={'enable_thinking':False}
        s=time.time()
        r=requests.post(u,json=payload,timeout=30)
        c=clean(r.json()['choices'][0]['message']['content'])
        # Retry if response too short after stripping think tags
        if is_ds and len(c)<20:
            msgs2=[{'role':'system','content':ANTI_THINK},{'role':'user','content':ANTI_THINK+'\n\n'+Q}]
            r=requests.post(u,json={'model':m,'messages':msgs2,'temperature':0.3,'max_tokens':800,'chat_template_kwargs':{'enable_thinking':False}},timeout=30)
            c=clean(r.json()['choices'][0]['message']['content'])
        return {'name':n,'r':c[:800],'t':round(time.time()-s,1),'ok':True}
    except: return {'name':n,'r':'FAIL','t':0,'ok':False}
with concurrent.futures.ThreadPoolExecutor(2) as ex:
    r2=ex.submit(q,'M2','http://192.168.1.26:1234/v1/chat/completions','qwen/qwen3-8b').result()
    r3=ex.submit(q,'M3','http://192.168.1.113:1234/v1/chat/completions','deepseek/deepseek-r1-0528-qwen3-8b').result()
print(f'  M2: {r2[\"t\"]}s')
print(f'  M3: {r3[\"t\"]}s')
json.dump({'m2':r2,'m3':r3},open('$OUTPUT_DIR/cluster_$TIMESTAMP.json','w'),indent=2,ensure_ascii=False)
" 2>/dev/null

# 2. BrowserOS IA Web (ChatGPT + Perplexity)
echo "🌐 IA Web..."
PERP_ID=$($CLI snap -p 13 2>/dev/null | grep "textbox" | head -1 | grep -oP '\[\d+\]' | head -1 | tr -d '[]')
[ -n "$PERP_ID" ] && $CLI fill $PERP_ID "$QUESTION" -p 13 2>/dev/null && $CLI key Enter -p 13 2>/dev/null && echo "  Perplexity: envoyé"

echo "⏳ Attente 20s..."
sleep 20

# 3. Collect + Save
echo "📊 Résultats sauvés: $OUTPUT_DIR/cluster_$TIMESTAMP.json"
echo "✅ Done"
