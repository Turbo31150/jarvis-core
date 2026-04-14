#!/usr/bin/env python3
"""Scanner automatique Codeur.com — projets IA + offres personnalisées via LLM local"""
import re, json, subprocess, requests
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = "8369376863:AAF-7YGDbun8mXWwqYJFj-eX6P78DeIu9Aw"
CHAT_ID = "2010747443"
KEYWORDS = ["ia", "agent", "claude", "automatisation", "python", "llm", "gpt", "chatbot",
            "machine learning", "rag", "n8n", "make", "zapier", "openai", "intelligence artificielle"]

PROFIL = """Franck Delmas, développeur IA & automatisation freelance.
Compétences: Python, Claude API, LangChain, agents IA, n8n, Make, LM Studio, Docker, Linux.
Projets: JARVIS OS multi-agents, pipelines IA locaux, automatisations PME.
Tarif: 400-800€/jour selon projet."""

def score(title, desc, budget):
    s = sum(2 for kw in KEYWORDS if kw in (title + desc).lower())
    s += 3 if any(x in budget for x in ["1 000", "2 000", "5 000", "10 000"]) else 0
    return min(s, 10)

def gen_proposal(title, desc, budget):
    """Génère offre personnalisée — OL1/gemma3 (rapide, gratuit)"""
    prompt = (
        f"Tu es {PROFIL}\n\n"
        f"Projet Codeur.com:\nTitre: {title}\nDescription: {desc[:500]}\nBudget: {budget}\n\n"
        f"Rédige une offre de candidature personnalisée en français (100-150 mots):\n"
        f"- Accroche qui montre que tu as lu CE projet précis\n"
        f"- Compétences directement liées au projet\n"
        f"- 1 exemple concret similaire que tu as réalisé\n"
        f"- Question ou suggestion experte\n"
        f"- Tarif/disponibilité\nStyle: professionnel, direct, pas de bullet points."
    )
    # Essai OL1 d'abord (le plus rapide)
    try:
        r = requests.post("http://127.0.0.1:11434/api/generate",
                          json={"model": "gemma3:4b", "prompt": prompt, "stream": False},
                          timeout=40)
        text = r.json().get("response", "").strip()
        if text:
            return text, "OL1/gemma3"
    except: pass
    # Fallback lm-ask.sh
    try:
        r = subprocess.run(["bash", "/home/turbo/jarvis/scripts/lm-ask.sh", "--max", "3000", prompt],
                           capture_output=True, text=True, timeout=120)
        if r.stdout.strip():
            return r.stdout.strip(), "M1/M2"
    except: pass
    return "[Erreur génération offre]", "ERR"

def notify(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

def scan():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.new_page()
        page.goto("https://www.codeur.com/projects/c/ia", wait_until="networkidle", timeout=15000)

        # Extraire URLs projets individuels
        urls = list(dict.fromkeys(
            "https://www.codeur.com" + el.get_attribute("href")
            for el in page.query_selector_all("a[href*='/projects/']")
            if re.search(r"/projects/\d+", el.get_attribute("href") or "")
            and "/providers/" not in (el.get_attribute("href") or "")
        ))
        print(f"Trouvé {len(urls)} projets IA")

        for url in urls[:25]:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=12000)
                h1 = page.query_selector("h1")
                title = h1.inner_text().strip() if h1 else page.title()
                body = page.inner_text("body")[:1200]

                # Déjà postulé ?
                if "Offre déposée" in body or "Modifier mon offre" in body:
                    print(f"[✅] Déjà postulé : {title[:55]}")
                    results.append({"title": title, "url": url, "applied": True})
                    continue

                # Budget
                budget = ""
                m = re.search(r"Budget[^:]*:\s*([^\n]+)", body)
                if m: budget = m.group(1).strip()

                sc = score(title, body, budget)
                print(f"[{sc:2d}] {title[:55]} | {budget}")

                if sc >= 6:
                    print(f"     ⚡ Génération offre personnalisée...")
                    proposal, backend = gen_proposal(title, body[:600], budget)
                    print(f"     [{backend}] {proposal[:150]}...")

                    results.append({"title": title, "url": url, "budget": budget,
                                    "score": sc, "proposal": proposal, "backend": backend})
                    notify(
                        f"🎯 <b>Projet IA (score {sc}/10)</b>\n"
                        f"<b>{title}</b>\n💶 {budget}\n🔗 {url}\n\n"
                        f"📝 <i>Offre générée [{backend}]:</i>\n{proposal[:700]}"
                    )
                else:
                    results.append({"title": title, "url": url, "budget": budget, "score": sc})

            except Exception as e:
                print(f"[ERR] {url}: {e}")

        page.close()
        browser.close()

    applied = sum(1 for r in results if r.get("applied"))
    pertinent = [r for r in results if not r.get("applied") and r.get("score", 0) >= 6]
    print(f"\n{'='*60}")
    print(f"Scanné: {len(results)} | Déjà postulés: {applied} | Offres générées: {len(pertinent)}")

    if pertinent:
        with open("/tmp/codeur-proposals.json", "w") as f:
            json.dump(pertinent, f, ensure_ascii=False, indent=2)
        print("Offres sauvées: /tmp/codeur-proposals.json")
    return results

if __name__ == "__main__":
    scan()
