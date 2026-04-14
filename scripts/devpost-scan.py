#!/usr/bin/env python3
"""Scanner Devpost hackathons IA + inscription automatique"""
import re, json, time, requests
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = "8369376863:AAF-7YGDbun8mXWwqYJFj-eX6P78DeIu9Aw"
CHAT_ID = "2010747443"

AI_TAGS = ["machine learning", "ai", "artificial intelligence", "llm", "nlp", 
           "data science", "python", "automation", "agent"]

def score_hackathon(title, desc, tags):
    text = (title + " " + desc + " " + " ".join(tags)).lower()
    s = sum(2 for kw in AI_TAGS if kw in text)
    return min(s, 10)

def notify(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

def gen_bio(title, desc):
    prompt = (f"Tu es Franck Delmas, développeur IA & automatisation expert.\n"
              f"Hackathon: {title}\nDescription: {desc[:300]}\n\n"
              f"Rédige une courte bio de candidature (50 mots) pour ce hackathon IA.\n"
              f"Mentionne: expertise Python/Claude API/agents IA, projet JARVIS OS, motivation.\n"
              f"Style: direct, enthousiaste, sans placeholders.")
    try:
        r = requests.post("http://127.0.0.1:11434/api/generate",
                          json={"model": "gemma3:4b", "prompt": prompt, "stream": False}, timeout=30)
        return r.json().get("response", "").strip()
    except:
        return "Développeur IA & automatisation, expert Python/Claude API/agents LLM. Créateur de JARVIS OS, système multi-agents. Passionné par l'IA appliquée aux défis réels."

def scan_devpost():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.new_page()
        
        # Filtrer hackathons ML/AI en ligne
        url = "https://devpost.com/hackathons?challenge_type=online&status=open&themes[]=Machine+Learning%2FAI"
        page.goto(url, wait_until="networkidle", timeout=20000)
        time.sleep(2)
        
        print(f"Page: {page.title()}")
        
        # Extraire les hackathons listés
        body = page.inner_text("body")
        
        # Trouver les liens hackathons
        links = page.query_selector_all('a[href*="devpost.com/"][href*="-"]')
        hack_urls = list(dict.fromkeys(
            l.get_attribute("href") for l in links
            if l.get_attribute("href") and "/hackathons" not in l.get_attribute("href")
            and "devpost.com" in l.get_attribute("href")
            and not any(x in l.get_attribute("href") for x in ["/software", "/user", "/login"])
        ))[:10]
        
        # Extraire aussi depuis le texte de la page directement
        hack_links = page.query_selector_all('[class*="hackathon"] a, article a, .tile a')
        for hl in hack_links[:20]:
            href = hl.get_attribute("href") or ""
            if href and href not in hack_urls:
                hack_urls.append(href)
        
        print(f"Hackathons IA trouvés: {len(hack_urls)}")
        
        for url in hack_urls[:8]:
            try:
                if not url.startswith("http"):
                    url = "https://devpost.com" + url
                page.goto(url, wait_until="domcontentloaded", timeout=12000)
                time.sleep(1)
                
                title = page.title().replace(" | Devpost", "").strip()
                body = page.inner_text("body")[:1500]
                
                # Tags
                tag_els = page.query_selector_all('[class*="tag"], [class*="theme"], [class*="interest"]')
                tags = [t.inner_text().strip() for t in tag_els[:10]]
                
                sc = score_hackathon(title, body, tags)
                print(f"[{sc}] {title[:60]}")
                
                if sc >= 4:
                    # Chercher bouton inscription
                    join_btn = (page.query_selector('a:has-text("Join Hackathon")') or
                                page.query_selector('a:has-text("Register")') or
                                page.query_selector('a:has-text("Apply")'))
                    
                    already = page.query_selector(':has-text("You are registered")') or \
                              page.query_selector(':has-text("Registered")')
                    
                    status = "inscrit" if already else ("inscription possible" if join_btn else "voir page")
                    print(f"  Status: {status} | Tags: {tags[:3]}")
                    
                    results.append({
                        "title": title, "url": page.url, "score": sc,
                        "tags": tags, "status": status
                    })
                    
                    notify(f"🏆 <b>Hackathon IA (score {sc}/10)</b>\n<b>{title}</b>\n"
                           f"Tags: {', '.join(tags[:3])}\n🔗 {page.url}\nStatut: {status}")
                    
            except Exception as e:
                print(f"  [ERR] {url}: {e}")
        
        page.close()
        browser.close()
    
    pertinents = [r for r in results if r["score"] >= 4]
    print(f"\nHackathons pertinents: {len(pertinents)}")
    with open("/tmp/devpost-results.json", "w") as f:
        json.dump(pertinents, f, ensure_ascii=False, indent=2)
    return results

if __name__ == "__main__":
    scan_devpost()
