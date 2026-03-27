#!/usr/bin/env python3
"""CDP Quick Actions — fast browser automation with selector caching."""
import json, asyncio, sys, os, time
from pathlib import Path
from datetime import datetime

try:
    import websockets
    import requests
except ImportError:
    print("pip install websockets requests")
    sys.exit(1)

CDP_PORT = int(os.getenv("BROWSEROS_DEBUG_PORT", "9222"))
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"
CACHE_FILE = Path(__file__).parent.parent / "data" / "browser-nav-cache.json"

def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {"version": 1, "sites": {}, "selectors": {}, "last_updated": ""}

def save_cache(cache):
    cache["last_updated"] = datetime.now().isoformat()
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))

def list_tabs():
    return requests.get(f"{CDP_BASE}/json", timeout=5).json()

def pages():
    return [t for t in list_tabs() if t.get("type") == "page"]

def find_tab(pattern):
    for t in pages():
        if pattern.lower() in (t.get("url","") + t.get("title","")).lower():
            return t
    return None

def new_tab(url):
    return requests.put(f"{CDP_BASE}/json/new?{url}", timeout=10).json()

def close_tab(tab_id):
    requests.get(f"{CDP_BASE}/json/close/{tab_id}", timeout=5)

async def cdp_eval(ws_url, js, timeout=10):
    async with websockets.connect(ws_url, max_size=10*1024*1024) as ws:
        msg = json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": js}})
        await ws.send(msg)
        resp = await asyncio.wait_for(ws.recv(), timeout=timeout)
        data = json.loads(resp)
        return data.get("result", {}).get("result", {}).get("value")

def evaluate(tab, js, timeout=10):
    return asyncio.get_event_loop().run_until_complete(cdp_eval(tab["webSocketDebuggerUrl"], js, timeout))

async def cdp_navigate(ws_url, url):
    async with websockets.connect(ws_url, max_size=10*1024*1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}))
        await asyncio.wait_for(ws.recv(), timeout=10)
        await asyncio.sleep(2)
        await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {"expression": "document.title + '|||' + window.location.href"}}))
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        val = json.loads(resp).get("result", {}).get("result", {}).get("value", "")
        parts = val.split("|||")
        return {"title": parts[0], "url": parts[1] if len(parts) > 1 else ""}

def navigate(tab, url):
    return asyncio.get_event_loop().run_until_complete(cdp_navigate(tab["webSocketDebuggerUrl"], url))

async def cdp_fill(ws_url, selector, value):
    js_val = json.dumps(value)
    js = f"""
    var el = document.querySelector('{selector}');
    if (el) {{
        el.focus();
        el.value = {js_val};
        el.dispatchEvent(new Event('input', {{bubbles:true}}));
        el.dispatchEvent(new Event('change', {{bubbles:true}}));
        'OK:' + el.value.length;
    }} else {{ 'NOT_FOUND'; }}
    """
    async with websockets.connect(ws_url, max_size=10*1024*1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": js}}))
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        return json.loads(resp).get("result", {}).get("result", {}).get("value")

def fill(tab, selector, value):
    return asyncio.get_event_loop().run_until_complete(cdp_fill(tab["webSocketDebuggerUrl"], selector, value))

async def cdp_click(ws_url, selector):
    js = f"var el = document.querySelector('{selector}'); el ? (el.click(), 'CLICKED') : 'NOT_FOUND';"
    async with websockets.connect(ws_url, max_size=10*1024*1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": js}}))
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        return json.loads(resp).get("result", {}).get("result", {}).get("value")

def click(tab, selector):
    return asyncio.get_event_loop().run_until_complete(cdp_click(tab["webSocketDebuggerUrl"], selector))

async def cdp_screenshot(ws_url, filepath):
    async with websockets.connect(ws_url, max_size=10*1024*1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
        resp = await asyncio.wait_for(ws.recv(), timeout=15)
        data = json.loads(resp).get("result", {}).get("data", "")
        if data:
            import base64
            Path(filepath).write_bytes(base64.b64decode(data))
            return f"Saved: {filepath}"
    return "FAILED"

def screenshot(tab, filepath):
    return asyncio.get_event_loop().run_until_complete(cdp_screenshot(tab["webSocketDebuggerUrl"], filepath))

def scan_page(tab):
    """Scan page structure and cache selectors for future fast access."""
    js = """
    JSON.stringify({
        title: document.title,
        url: window.location.href,
        forms: Array.from(document.querySelectorAll('form')).map(f => ({
            id: f.id, action: f.action?.substring(0,80), method: f.method
        })),
        inputs: Array.from(document.querySelectorAll('input,textarea,select')).map(e => ({
            tag: e.tagName, type: e.type, name: e.name, id: e.id,
            placeholder: (e.placeholder||'').substring(0,40),
            label: e.closest('.form-group')?.querySelector('label')?.textContent?.trim()?.substring(0,40) || ''
        })).filter(e => e.name || e.id),
        buttons: Array.from(document.querySelectorAll('button,input[type=submit],a.btn')).map(b => ({
            tag: b.tagName, type: b.type, text: b.textContent?.trim()?.substring(0,40),
            id: b.id, class: b.className?.substring(0,40), href: b.href?.substring(0,80)
        })).filter(b => b.text),
        links: Array.from(document.querySelectorAll('a')).filter(a => 
            a.href && (a.href.includes('edit') || a.href.includes('profil') || a.href.includes('settings') || a.href.includes('account'))
        ).map(a => ({text: a.textContent?.trim()?.substring(0,40), href: a.href?.substring(0,100)})).slice(0,15)
    })
    """
    result = evaluate(tab, js, timeout=10)
    if result:
        data = json.loads(result)
        # Cache it
        cache = load_cache()
        domain = data["url"].split("/")[2] if "/" in data["url"] else "unknown"
        cache["sites"][domain] = {
            "last_scan": datetime.now().isoformat(),
            "page": data["url"],
            "title": data["title"],
            "forms": data["forms"],
            "inputs": data["inputs"],
            "buttons": data["buttons"],
            "links": data["links"]
        }
        save_cache(cache)
        return data
    return None

if __name__ == "__main__":
    print(f"CDP Quick Actions — {CDP_BASE}")
    tabs = pages()
    print(f"{len(tabs)} page tabs:")
    for t in tabs:
        print(f"  {t['title'][:50]} -> {t['url'][:60]}")

# === AUTO-LEARNING NAVIGATION ===

def smart_find_tab(pattern):
    """Find tab with fallback: exact URL > domain > title. Learns from cache."""
    tab = find_tab(pattern)
    if tab:
        return tab
    # Try opening from cache
    cache = load_cache()
    for domain, data in cache.get("sites", {}).items():
        if pattern.lower() in domain.lower():
            url = data.get("page", "")
            if url:
                t = new_tab(url)
                import time; time.sleep(3)
                return find_tab(pattern)
    return None

def smart_scan(tab):
    """Scan + learn: stores forms, inputs, buttons, links, and page structure."""
    data = scan_page(tab)
    if not data:
        return None
    # Auto-detect page type
    url = data.get("url", "")
    domain = url.split("/")[2] if "/" in url else "unknown"
    path = "/".join(url.split("/")[3:5]) if "/" in url else ""
    cache = load_cache()
    
    # Store path-specific selectors
    key = f"{domain}/{path}" if path else domain
    cache["selectors"][key] = {
        "forms": [f["id"] for f in data.get("forms", []) if f.get("id")],
        "inputs": {i["name"] or i["id"]: {"tag": i["tag"], "type": i.get("type",""), "label": i.get("label","")} 
                   for i in data.get("inputs", []) if i.get("name") or i.get("id")},
        "buttons": {b["text"]: {"tag": b["tag"], "class": b.get("class",""), "id": b.get("id","")} 
                    for b in data.get("buttons", []) if b.get("text")},
        "edit_links": {l["text"]: l["href"] for l in data.get("links", []) if l.get("text")},
        "scanned_at": datetime.now().isoformat()
    }
    save_cache(cache)
    return data

def smart_fill_form(tab, fields_dict):
    """Fill multiple form fields at once using cached selectors."""
    results = {}
    for field_name, value in fields_dict.items():
        # Try by name, then by id
        for selector in [f'[name="{field_name}"]', f'#{field_name}']:
            r = fill(tab, selector, value)
            if r and "OK" in str(r):
                results[field_name] = "OK"
                break
        else:
            results[field_name] = "NOT_FOUND"
    return results

def smart_click_button(tab, button_text):
    """Click button by text content, with fuzzy matching."""
    js = f"""
    var btns = Array.from(document.querySelectorAll('button, input[type=submit], a.btn, a.btn-primary, a.btn-success'));
    var target = btns.find(function(b) {{
        return b.textContent.trim().toLowerCase().includes('{button_text.lower()}');
    }});
    if (target) {{ target.click(); 'CLICKED:' + target.textContent.trim().substring(0,40); }}
    else {{ 'NOT_FOUND'; }}
    """
    return evaluate(tab, js)

def wait_loaded(tab, timeout=10):
    """Wait for page to be fully loaded."""
    import time
    start = time.time()
    while time.time() - start < timeout:
        state = evaluate(tab, "document.readyState")
        if state == "complete":
            return True
        time.sleep(0.5)
    return False

def get_full_text(tab, selector="body"):
    """Get full text content of an element."""
    return evaluate(tab, f"document.querySelector('{selector}')?.innerText?.substring(0,5000) || ''")

def get_auth_status(tab):
    """Check if logged in on current site."""
    return evaluate(tab, """
    JSON.stringify({
        hasLogout: !!document.querySelector('a[href*=sign_out], a[href*=logout], a[href*=déconnexion]'),
        hasLogin: !!document.querySelector('a[href*=sign_in], a[href*=login], a[href*=connexion]'),
        cookies: document.cookie.length > 0
    })
    """)


# === SPEED ROUTES ===
ROUTES_FILE = Path(__file__).parent.parent / "data" / "browser-speed-routes.json"

def load_routes():
    if ROUTES_FILE.exists():
        return json.loads(ROUTES_FILE.read_text())
    return {}

def save_routes(routes):
    ROUTES_FILE.write_text(json.dumps(routes, indent=2, ensure_ascii=False))

def quick_login(site, credentials):
    """One-shot login using cached speed routes."""
    routes = load_routes()
    route = routes.get(site, {}).get("login")
    if not route:
        return f"No login route for {site}"
    
    tab = find_tab(site) or new_tab(route["url"])
    import time; time.sleep(2)
    tabs_list = pages()
    tab = next((t for t in tabs_list if site in t.get("url","")), tab)
    
    if "sign_in" not in tab.get("url","") and "login" not in tab.get("url",""):
        result = navigate(tab, route["url"])
        time.sleep(2)
        tabs_list = pages()
        tab = next((t for t in tabs_list if site in t.get("url","")), tab)
    
    results = {}
    for field_name, selector in route.get("fields", {}).items():
        if field_name in credentials:
            r = fill(tab, selector, credentials[field_name])
            results[field_name] = r
    
    # Click submit
    submit_sel = route.get("submit", "input[type=submit]")
    click(tab, submit_sel)
    time.sleep(3)
    
    # Check success
    tabs_list = pages()
    tab = next((t for t in tabs_list if site in t.get("url","")), None)
    if tab and route.get("success_indicator"):
        check = evaluate(tab, f"!!document.querySelector('{route['success_indicator']}')")
        results["logged_in"] = check
    
    # Learn: update cache with auth status
    cache = load_cache()
    cache["sites"][site] = cache.get("sites", {}).get(site, {})
    cache["sites"][site]["authenticated"] = True
    cache["sites"][site]["login_time"] = datetime.now().isoformat()
    save_cache(cache)
    
    return results

def quick_navigate(site, page_name):
    """Navigate to a known page using speed routes."""
    routes = load_routes()
    route = routes.get(site, {}).get(page_name)
    if not route:
        return None
    
    tab = find_tab(site) or new_tab(route["url"])
    import time; time.sleep(1)
    tabs_list = pages()
    tab = next((t for t in tabs_list if site in t.get("url","")), tab)
    
    if route["url"] not in tab.get("url", ""):
        navigate(tab, route["url"])
        time.sleep(2)
        tabs_list = pages()
        tab = next((t for t in tabs_list if site in t.get("url","")), tab)
    
    # Auto-scan and cache
    smart_scan(tab)
    return tab

def quick_fill_and_submit(site, page_name, data_dict):
    """Fill form and submit using speed routes."""
    routes = load_routes()
    route = routes.get(site, {}).get(page_name)
    if not route:
        return "No route found"
    
    tab = quick_navigate(site, page_name)
    if not tab:
        return "Navigation failed"
    
    import time; time.sleep(1)
    
    results = {}
    for field_name, value in data_dict.items():
        selector = route.get("fields", {}).get(field_name)
        if selector:
            r = fill(tab, selector, value)
            results[field_name] = r
    
    # Submit
    submit_sel = route.get("submit", "input[type=submit], button[type=submit]")
    click_result = click(tab, submit_sel)
    results["submit"] = click_result
    time.sleep(2)
    
    return results

def learn_page(tab):
    """Deep scan page and auto-add to speed routes."""
    data = smart_scan(tab)
    if not data:
        return None
    
    url = data.get("url", "")
    domain = url.split("/")[2] if "/" in url else "unknown" 
    path_parts = url.split("/")[3:]
    page_key = path_parts[0] if path_parts else "home"
    
    routes = load_routes()
    if domain not in routes:
        routes[domain] = {}
    
    # Build route from scanned data
    route = {
        "url": url,
        "fields": {},
        "buttons": {},
        "learned_at": datetime.now().isoformat()
    }
    
    for inp in data.get("inputs", []):
        name = inp.get("name") or inp.get("id")
        if name:
            selector = f'#{inp["id"]}' if inp.get("id") else f'[name="{inp["name"]}"]'
            route["fields"][name] = selector
    
    for btn in data.get("buttons", []):
        text = btn.get("text", "")
        if text:
            route["buttons"][text] = btn.get("class", "")
    
    routes[domain][page_key] = route
    save_routes(routes)
    
    return route
