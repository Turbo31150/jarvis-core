#!/usr/bin/env python3
"""
JARVIS Page Learner — Apprend la structure complète d'une page web.
Extrait: HTML source, balises clés, formulaires, boutons, liens, inputs.
Génère un fichier de navigation rapide pour les agents.

Usage:
    python3 page_learner.py <url> [--cdp PORT]
    python3 page_learner.py https://www.codeur.com/projects/480392 --cdp 9222

    # En Python:
    from scripts.page_learner import PageLearner
    learner = PageLearner(cdp_port=9108)
    page = learner.learn("https://www.codeur.com/projects/480392")
    learner.click(page, "Faire une offre")
    learner.fill(page, "offer[amount]", "8500")
    learner.submit(page)
"""

import asyncio
import aiohttp
import json
import sys
import os
from pathlib import Path
from typing import Optional

CACHE_DIR = Path(os.path.expanduser("~/IA/Core/jarvis/data/page_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class PageLearner:
    def __init__(self, cdp_port: int = 9108):
        self.cdp = f"http://127.0.0.1:{cdp_port}"
        self._ws = None
        self._session = None
        self._mid = 0

    async def _connect(self, tab_id: Optional[str] = None):
        self._session = aiohttp.ClientSession()
        async with self._session.get(f"{self.cdp}/json") as r:
            tabs = await r.json()
        pages = [t for t in tabs if t.get("type") == "page"]
        if tab_id:
            tab = next((t for t in pages if tab_id in t["id"]), pages[0])
        else:
            tab = pages[0]
        self._ws = await self._session.ws_connect(tab["webSocketDebuggerUrl"])
        await self._send("Page.enable")
        await self._send("Runtime.enable")
        return tab

    async def _send(self, method, params=None):
        self._mid += 1
        await self._ws.send_json({"id": self._mid, "method": method, "params": params or {}})
        for _ in range(100):
            try:
                m = await asyncio.wait_for(self._ws.receive(), timeout=20)
                if m.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("id") == self._mid:
                        return d.get("result", {})
            except asyncio.TimeoutError:
                return {}
        return {}

    async def _close(self):
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def learn(self, url: str, navigate: bool = True) -> dict:
        """Apprend la structure complète d'une page."""
        tab = await self._connect()

        if navigate:
            await self._send("Page.navigate", {"url": url})
            await asyncio.sleep(5)

        # Extract EVERYTHING
        r = await self._send("Runtime.evaluate", {"expression": """
            (() => {
                const page = {
                    url: window.location.href,
                    title: document.title,
                    buttons: [],
                    links: [],
                    forms: [],
                    inputs: [],
                    textareas: [],
                    selects: [],
                    headings: [],
                    images: [],
                    interactable: []
                };

                // Buttons (button + a.btn + input[type=submit])
                document.querySelectorAll('button, a.btn, a.btn-primary, a.btn-outline-primary, input[type=submit], input[type=button]').forEach((el, i) => {
                    page.buttons.push({
                        id: i,
                        tag: el.tagName,
                        text: el.textContent.trim().substring(0, 80),
                        value: el.value || '',
                        href: el.href || '',
                        class: el.className.substring(0, 100),
                        name: el.name || '',
                        type: el.type || '',
                        visible: el.offsetParent !== null,
                        rect: el.getBoundingClientRect().toJSON(),
                        selector: el.id ? '#' + el.id : (el.name ? '[name=\"' + el.name + '\"]' : el.tagName.toLowerCase() + ':nth-of-type(' + (Array.from(el.parentNode.children).indexOf(el) + 1) + ')')
                    });
                });

                // Links
                document.querySelectorAll('a[href]').forEach((el, i) => {
                    if (i > 50) return;
                    page.links.push({
                        text: el.textContent.trim().substring(0, 60),
                        href: el.href.substring(0, 120),
                        class: el.className.substring(0, 60)
                    });
                });

                // Forms
                document.querySelectorAll('form').forEach((el, i) => {
                    page.forms.push({
                        id: i,
                        action: el.action.substring(0, 120),
                        method: el.method,
                        fields: Array.from(el.querySelectorAll('input, textarea, select')).map(f => ({
                            tag: f.tagName, type: f.type, name: f.name, id: f.id,
                            placeholder: f.placeholder, value: f.value.substring(0, 50),
                            required: f.required
                        }))
                    });
                });

                // All inputs
                document.querySelectorAll('input:not([type=hidden]), textarea, select').forEach((el, i) => {
                    page.inputs.push({
                        tag: el.tagName, type: el.type || '', name: el.name, id: el.id,
                        placeholder: el.placeholder || '', value: el.value.substring(0, 50),
                        visible: el.offsetParent !== null,
                        rect: el.getBoundingClientRect().toJSON()
                    });
                });

                // Headings
                document.querySelectorAll('h1, h2, h3').forEach(el => {
                    page.headings.push(el.textContent.trim().substring(0, 100));
                });

                return JSON.stringify(page);
            })()
        """})

        page_data = json.loads(r.get("result", {}).get("value", "{}"))

        # Save to cache
        safe_name = url.split("/")[-1][:50].replace("?", "_").replace("&", "_")
        cache_file = CACHE_DIR / f"{safe_name}.json"
        with open(cache_file, "w") as f:
            json.dump(page_data, f, indent=2, ensure_ascii=False)

        await self._close()
        return page_data

    async def click_by_text(self, text: str):
        """Clique sur un élément par son texte."""
        tab = await self._connect()
        r = await self._send("Runtime.evaluate", {"expression": f"""
            (() => {{
                const els = Array.from(document.querySelectorAll('a, button, input[type=submit]'));
                const el = els.find(e => e.textContent.trim().includes('{text}') || e.value === '{text}');
                if (el) {{ el.scrollIntoView({{block:'center'}}); el.click(); return 'CLICKED: ' + el.textContent.trim().substring(0,40); }}
                return 'NOT_FOUND';
            }})()
        """})
        result = r.get("result", {}).get("value", "")
        await self._close()
        return result

    async def fill_field(self, name_or_selector: str, value: str):
        """Remplit un champ par nom ou sélecteur."""
        tab = await self._connect()
        val_js = json.dumps(value)
        r = await self._send("Runtime.evaluate", {"expression": f"""
            (() => {{
                const el = document.querySelector('[name="{name_or_selector}"]')
                    || document.querySelector('#{name_or_selector}')
                    || document.querySelector('{name_or_selector}');
                if (el) {{
                    el.focus();
                    el.value = {val_js};
                    el.dispatchEvent(new Event('input', {{bubbles:true}}));
                    el.dispatchEvent(new Event('change', {{bubbles:true}}));
                    return 'FILLED: ' + el.name + ' = ' + el.value.substring(0,30);
                }}
                return 'NOT_FOUND: {name_or_selector}';
            }})()
        """})
        result = r.get("result", {}).get("value", "")
        await self._close()
        return result

    async def fill_form_and_submit(self, fields: dict, submit_text: str = "Publier"):
        """Remplit un formulaire complet et soumet."""
        tab = await self._connect()

        # Fill all fields
        for name, value in fields.items():
            val_js = json.dumps(value)
            await self._send("Runtime.evaluate", {"expression": f"""
                (() => {{
                    const el = document.querySelector('[name="{name}"]') || document.querySelector('#{name}') || document.querySelector('textarea');
                    if (el) {{
                        el.value = {val_js};
                        el.dispatchEvent(new Event('input', {{bubbles:true}}));
                        el.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }}
                }})()
            """})

        await asyncio.sleep(1)

        # Submit
        r = await self._send("Runtime.evaluate", {"expression": f"""
            (() => {{
                const btns = Array.from(document.querySelectorAll('input[type=submit], button[type=submit], button'));
                const btn = btns.find(b => (b.value || b.textContent).includes('{submit_text}'));
                if (btn) {{ btn.click(); return 'SUBMITTED'; }}
                const form = document.querySelector('form');
                if (form) {{ form.submit(); return 'FORM_SUBMITTED'; }}
                return 'NO_SUBMIT';
            }})()
        """})
        result = r.get("result", {}).get("value", "")
        await self._close()
        return result

    async def post_codeur_offer(self, project_url: str, amount: str, delay: str, message: str):
        """Workflow complet: naviguer → cliquer Faire une offre → remplir → publier."""
        tab = await self._connect()

        # Navigate
        print(f"[1] Navigating to {project_url[:50]}...")
        await self._send("Page.navigate", {"url": project_url})
        await asyncio.sleep(6)

        # Check if already offered
        r = await self._send("Runtime.evaluate", {"expression": """
            JSON.stringify({
                offered: document.body.innerText.includes('Modifier mon offre'),
                canOffer: !!Array.from(document.querySelectorAll('a,button')).find(e=>e.textContent.includes('Faire une offre')),
                title: document.title.substring(0,50)
            })
        """})
        state = json.loads(r.get("result", {}).get("value", "{}"))
        print(f"[2] {state.get('title')} | offered:{state.get('offered')} | canOffer:{state.get('canOffer')}")

        if state.get("offered"):
            print("[3] DÉJÀ POSTULÉ")
            await self._close()
            return "ALREADY_OFFERED"

        if not state.get("canOffer"):
            print("[3] PAS DE BOUTON — pas connecté ?")
            await self._close()
            return "NO_BUTTON"

        # Click Faire une offre
        print("[3] Clicking 'Faire une offre'...")
        await self._send("Runtime.evaluate", {"expression": """
            Array.from(document.querySelectorAll('a,button')).find(e=>e.textContent.includes('Faire une offre'))?.click()
        """})

        # Wait for form with MutationObserver
        r = await self._send("Runtime.evaluate", {"expression": """
            new Promise(resolve => {
                if (document.querySelector('textarea')) { resolve('INSTANT'); return; }
                const obs = new MutationObserver(() => {
                    if (document.querySelector('textarea')) { obs.disconnect(); resolve('APPEARED'); }
                });
                obs.observe(document.body, {childList:true, subtree:true});
                setTimeout(() => { obs.disconnect(); resolve('TIMEOUT'); }, 15000);
            })
        """, "awaitPromise": True})
        form_status = r.get("result", {}).get("value", "TIMEOUT")
        print(f"[4] Form: {form_status}")

        if form_status == "TIMEOUT":
            # Try scrolling — form might be below
            await self._send("Runtime.evaluate", {"expression": "window.scrollTo(0, document.body.scrollHeight)"})
            await asyncio.sleep(2)
            r = await self._send("Runtime.evaluate", {"expression": "!!document.querySelector('textarea')"})
            if not r.get("result", {}).get("value"):
                print("[5] FORM NOT FOUND")
                await self._close()
                return "FORM_TIMEOUT"

        # Fill form
        msg_js = json.dumps(message)
        r = await self._send("Runtime.evaluate", {"expression": f"""
            (() => {{
                const a = document.querySelector('input[name="offer[amount]"]');
                const d = document.querySelector('input[name="offer[duration]"]');
                const t = document.querySelector('textarea');
                if(a) {{ a.value='{amount}'; a.dispatchEvent(new Event('input',{{bubbles:true}})); a.dispatchEvent(new Event('change',{{bubbles:true}})); }}
                if(d) {{ d.value='{delay}'; d.dispatchEvent(new Event('input',{{bubbles:true}})); d.dispatchEvent(new Event('change',{{bubbles:true}})); }}
                if(t) {{ t.value={msg_js}; t.dispatchEvent(new Event('input',{{bubbles:true}})); t.dispatchEvent(new Event('change',{{bubbles:true}})); }}
                return 'A='+(a?.value||'X')+' D='+(d?.value||'X')+' T='+(t?.value?.length||0);
            }})()
        """})
        print(f"[5] Fill: {r.get('result', {}).get('value', '')}")

        # Submit
        r = await self._send("Runtime.evaluate", {"expression": """
            (() => {
                const b = document.querySelector('input[value="Publier mon offre"]');
                if(b) { b.click(); return 'PUBLISHED'; }
                return 'NO_SUBMIT';
            })()
        """})
        print(f"[6] Submit: {r.get('result', {}).get('value', '')}")
        await asyncio.sleep(5)

        # Verify
        r = await self._send("Runtime.evaluate", {"expression": """
            document.body.innerText.includes('Offre déposée') || document.body.innerText.includes('Super offre') || document.body.innerText.includes('Modifier mon offre')
            ? 'SUCCESS' : 'CHECK: ' + document.title.substring(0,40)
        """})
        result = r.get("result", {}).get("value", "")
        print(f"[7] Result: {result}")

        await self._close()
        return result


# CLI
async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    url = sys.argv[1]
    port = 9108
    if "--cdp" in sys.argv:
        port = int(sys.argv[sys.argv.index("--cdp") + 1])

    learner = PageLearner(cdp_port=port)

    if sys.argv[1] == "learn":
        url = sys.argv[2]
        page = await learner.learn(url)
        print(f"\nPage: {page['title']}")
        print(f"URL: {page['url']}")
        print(f"\nButtons ({len(page['buttons'])}):")
        for b in page['buttons']:
            if b['visible']:
                print(f"  [{b['id']}] {b['text'][:50]} | {b['selector']}")
        print(f"\nForms ({len(page['forms'])}):")
        for f in page['forms']:
            print(f"  Form → {f['action'][:60]}")
            for field in f['fields']:
                print(f"    {field['tag']}:{field['type']} name={field['name']} id={field['id']}")
        print(f"\nInputs ({len(page['inputs'])}):")
        for inp in page['inputs']:
            if inp['visible']:
                print(f"  {inp['tag']}:{inp['type']} name={inp['name']} placeholder={inp['placeholder'][:30]}")

    elif sys.argv[1] == "click":
        text = sys.argv[2]
        result = await learner.click_by_text(text)
        print(result)

    elif sys.argv[1] == "offer":
        url = sys.argv[2]
        amount = sys.argv[3]
        delay = sys.argv[4]
        msg = sys.argv[5] if len(sys.argv) > 5 else "Bonjour, je suis intéressé par ce projet."
        result = await learner.post_codeur_offer(url, amount, delay, msg)
        print(f"Final: {result}")

    else:
        page = await learner.learn(url)
        print(json.dumps(page, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
