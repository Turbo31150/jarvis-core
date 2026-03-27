#!/usr/bin/env python3
"""Execute after login on Codeur/LinkedIn — posts proposals, checks notifications."""
import sys, json, time
sys.path.insert(0, "/home/turbo/IA/Core/jarvis/scripts")
import cdp
from pathlib import Path

PROPOSAL_FILE = Path("/home/turbo/IA/Core/jarvis/data/proposal-woocommerce-ia.txt")

def codeur_actions():
    """Post proposal + check projects."""
    tab = cdp.find_tab("codeur.com")
    if not tab:
        print("No Codeur tab")
        return
    
    # Navigate to the WooCommerce project
    print("Navigating to WooCommerce project...")
    cdp.navigate(tab, "https://www.codeur.com/projects/480360-expert-ia-automatisation-generation-de-fiches-produits-woocommerce")
    time.sleep(3)
    
    # Refresh tab ref
    tabs = cdp.pages()
    tab = next((t for t in tabs if '480360' in t.get('url','')), None)
    if not tab:
        print("Could not find project page")
        return
    
    # Look for offer form
    result = cdp.evaluate(tab, """
        var btns = Array.from(document.querySelectorAll('a,button'));
        var offer = btns.find(b => b.textContent.toLowerCase().includes('offre'));
        offer ? offer.tagName + '|' + offer.textContent.trim().substring(0,40) + '|' + (offer.href||'') : 'NO_OFFER_BTN';
    """)
    print(f"Offer button: {result}")
    
    if result and 'sign_up' not in str(result) and 'NO_OFFER' not in str(result):
        # Click to make offer
        cdp.smart_click_button(tab, "offre")
        time.sleep(3)
        
        # Fill proposal
        proposal = PROPOSAL_FILE.read_text()
        tabs = cdp.pages()
        tab = next((t for t in tabs if 'codeur' in t.get('url','')), None)
        if tab:
            cdp.smart_scan(tab)
            # Try to fill textarea
            for sel in ['textarea[name*=message]', 'textarea[name*=body]', 'textarea#offer_body', 'textarea']:
                r = cdp.fill(tab, sel, proposal)
                if r and 'OK' in str(r):
                    print(f"Proposal filled ({sel})")
                    break
            print("Ready to submit — waiting for confirmation")
    else:
        print("Need to be logged in to make offer")

def linkedin_actions():
    """Check notifications + profile stats."""
    tab = cdp.find_tab("linkedin.com")
    if not tab:
        print("No LinkedIn tab")
        return
    
    # Go to notifications
    cdp.navigate(tab, "https://www.linkedin.com/notifications/")
    time.sleep(3)
    tabs = cdp.pages()
    tab = next((t for t in tabs if 'linkedin' in t.get('url','')), None)
    if tab:
        cdp.smart_scan(tab)
        title = cdp.evaluate(tab, "document.title")
        print(f"LinkedIn: {title}")

if __name__ == "__main__":
    print("=== Post-Login Actions ===")
    tabs = cdp.pages()
    
    codeur_auth = False
    linkedin_auth = False
    
    for tab in tabs:
        url = tab.get('url','')
        if 'codeur.com' in url:
            try:
                auth = json.loads(cdp.get_auth_status(tab))
                codeur_auth = auth.get('hasLogout', False)
            except: pass
        if 'linkedin.com' in url:
            try:
                auth = json.loads(cdp.get_auth_status(tab))
                linkedin_auth = auth.get('hasLogout', False)
            except: pass
    
    if codeur_auth:
        print("\n--- Codeur Actions ---")
        codeur_actions()
    else:
        print("Codeur: not logged in")
    
    if linkedin_auth:
        print("\n--- LinkedIn Actions ---")
        linkedin_actions()
    else:
        print("LinkedIn: not logged in")
