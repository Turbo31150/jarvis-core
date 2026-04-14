#!/usr/bin/env python3
"""Soumet les 2 meilleures offres Codeur.com via Playwright CDP"""
import json, re, time, requests
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = "8369376863:AAF-7YGDbun8mXWwqYJFj-eX6P78DeIu9Aw"
CHAT_ID = "2010747443"

def clean_proposal(text):
    """Nettoyer les placeholders génériques"""
    text = re.sub(r'\[Votre Numéro de Téléphone\]', '', text)
    text = re.sub(r'\[Votre Site Web/Portfolio.*?\]', 'https://github.com/turbo31150', text)
    text = re.sub(r'\[Votre.*?\]', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def notify(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

def submit_proposal(page, url, proposal):
    """Navigue sur le projet et soumet l'offre"""
    print(f"\n→ Navigation vers {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=15000)
    time.sleep(2)

    body = page.inner_text("body")

    # Vérifier si déjà postulé
    if "Offre déposée" in body or "Modifier mon offre" in body:
        print("  ⚠️  Déjà postulé sur ce projet")
        return False, "déjà postulé"

    # Vérifier si projet fermé
    if "Fermé" in body or "Annulé" in body:
        print("  ⚠️  Projet fermé ou annulé")
        return False, "projet fermé"

    # Chercher le bouton "Postuler" / "Déposer une offre"
    postuler_btn = None
    for selector in [
        'a[href*="postuler"]', 'a[href*="new_offer"]', 'a[href*="offre"]',
        'button:has-text("Postuler")', 'a:has-text("Postuler")',
        'a:has-text("Déposer une offre")', 'button:has-text("Déposer")',
        '[data-action*="postuler"]', '.btn-postuler'
    ]:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                postuler_btn = el
                print(f"  ✓ Bouton trouvé: {selector}")
                break
        except: continue

    if not postuler_btn:
        # Screenshot pour debug
        page.screenshot(path="/tmp/codeur-debug.png")
        print("  ❌ Bouton postuler non trouvé — screenshot: /tmp/codeur-debug.png")
        # Chercher via snapshot accessibility
        snap = page.accessibility.snapshot()
        print(f"  Titre page: {page.title()}")
        return False, "bouton introuvable"

    # Cliquer postuler
    postuler_btn.click()
    time.sleep(2)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(1)

    # Chercher le textarea de l'offre
    textarea = None
    for sel in ['textarea[name*="description"]', 'textarea[name*="message"]',
                'textarea[name*="offer"]', 'textarea[name*="body"]',
                'textarea[id*="description"]', 'textarea[id*="message"]',
                'textarea', '[contenteditable="true"]']:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                textarea = el
                print(f"  ✓ Textarea trouvé: {sel}")
                break
        except: continue

    if not textarea:
        page.screenshot(path="/tmp/codeur-form-debug.png")
        print("  ❌ Textarea non trouvé — screenshot: /tmp/codeur-form-debug.png")
        return False, "formulaire introuvable"

    # Effacer et remplir le textarea
    textarea.click()
    textarea.fill("")
    time.sleep(0.5)
    textarea.type(proposal, delay=10)
    time.sleep(1)
    print(f"  ✓ Offre saisie ({len(proposal)} caractères)")

    # Chercher et cliquer le bouton de soumission
    submit_btn = None
    for sel in ['button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Envoyer")', 'button:has-text("Soumettre")',
                'button:has-text("Postuler")', 'button:has-text("Valider")',
                '[data-action*="submit"]']:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                submit_btn = el
                print(f"  ✓ Submit trouvé: {sel}")
                break
        except: continue

    if not submit_btn:
        page.screenshot(path="/tmp/codeur-submit-debug.png")
        print("  ❌ Bouton submit non trouvé — screenshot: /tmp/codeur-submit-debug.png")
        return False, "submit introuvable"

    # Soumettre
    page.screenshot(path="/tmp/codeur-before-submit.png")
    submit_btn.click()
    time.sleep(3)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(2)

    # Vérifier succès
    new_body = page.inner_text("body")
    if "Offre déposée" in new_body or "offre a été" in new_body.lower() or "envoyée" in new_body.lower():
        page.screenshot(path="/tmp/codeur-success.png")
        print("  ✅ OFFRE SOUMISE AVEC SUCCÈS")
        return True, "succès"
    else:
        page.screenshot(path="/tmp/codeur-after-submit.png")
        print("  ⚠️  Statut incertain après soumission")
        return True, "soumis (vérifier)"

def main():
    with open("/tmp/codeur-proposals.json") as f:
        proposals = json.load(f)
    proposals.sort(key=lambda x: x.get("score", 0), reverse=True)
    top2 = proposals[:2]

    print(f"Soumission des {len(top2)} meilleures offres...")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = ctx.new_page()

        for i, proj in enumerate(top2, 1):
            title = proj["title"]
            url = proj["url"]
            proposal = clean_proposal(proj["proposal"])

            print(f"\n{'='*60}")
            print(f"[{i}/2] {title[:55]}")
            print(f"Score: {proj['score']}/10 | Budget: {proj.get('budget','?')}")

            success, status = submit_proposal(page, url, proposal)

            if success:
                notify(
                    f"✅ <b>Offre soumise sur Codeur.com</b>\n"
                    f"<b>{title}</b>\n"
                    f"💶 {proj.get('budget', '?')}\n"
                    f"🔗 {url}\n"
                    f"Statut: {status}"
                )
            else:
                notify(
                    f"⚠️ <b>Échec soumission Codeur.com</b>\n"
                    f"<b>{title}</b>\n"
                    f"Raison: {status}"
                )
            time.sleep(3)

        page.close()
        browser.close()

if __name__ == "__main__":
    main()
