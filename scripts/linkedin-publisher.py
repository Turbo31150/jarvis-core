#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linkedin-publisher.py — Publie des posts LinkedIn via Chrome CDP (port 9222)

Génère un post IA/tech quotidien avec M2/qwen3.5-35b, publie via Playwright/CDP.
Logger dans jarvis-master.db table linkedin_posts.

Cron : 0 9 * * * pour publication à 9h00

Usage:
    python linkedin-publisher.py --topic "LLMs locaux"
    python linkedin-publisher.py --draft     # Génère mais ne publie pas
    python linkedin-publisher.py --status    # Status last post
"""

import os
import sys
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

# Configuration
DB_PATH = "/home/turbo/jarvis-master.db"
BROWSER_CDP_URL = "http://127.0.0.1:9222"
MODEL_M2 = "deepseek-coder-v2-lite-instruct"  # Qualite + speed

# Chemin script M2 (via exec)
M2_SCRIPT = """curl.exe -s http://192.168.1.26:1234/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer LMSTUDIO_KEY_M2_REDACTED" \\
  -d '{{"model":"{{MODEL}}","messages":[{{"role":"user","content":"{{PROMPT}}"}}],"max_tokens":2048}}' | \\.choices[0]\\.message.content"""


def get_db_connection():
    """Connexion SQLite."""
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Base de données non trouvée: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    return conn


def init_db(conn):
    """Initialise la table linkedin_posts si absente."""
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS linkedin_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        content TEXT NOT NULL,
        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        published BOOLEAN DEFAULT FALSE,
        status TEXT DEFAULT 'pending',
        error_message TEXT
    )""")
    conn.commit()


def generate_post(topic: str) -> dict:
    """Génère un post LinkedIn avec M2."""
    prompt = f"""Tu es content-creator pour Franc Delmas (Turbo), Ingénieur IA & Freelance, 
spécialisé LLMs locaux, automatisation Python, JARVIS OS, trading algo.

Sujet: {topic}

Génère un post LinkedIn professionnel avec ce format EXACT:
---
[ACCROCHE] — 1 ligne percutante avec emoji (💡⚙️🚀)
[CONTENU] — 5-7 points courts, techniques mais accessibles (•→✓)
[CALL-TO-ACTION] — question engageante ou invite commenter/sharer
[HASHTAGS] — #IA #Python #LLM #Freelance #Automatisation (3-5 max)
---

TON: Direct, technique, authentique. Pas de "Great question!", juste du fond.
Exemples concrets + chiffres quand possible.

Réponds UNIQUEMENT avec le format JSON suivant:
{{
  "accroche": "...",
  "points": ["point1", "point2", ...],
  "cta": "...",
  "hashtags": ["#IA", "#Python", ...]
}}
"""
    
    # Execute via exec (shell tool)
    result = subprocess.run(
        ["exec", "-c", M2_SCRIPT.replace("{{MODEL}}", MODEL_M2).replace("{{PROMPT}}", prompt)],
        capture_output=True, text=True, env=None
    )
    
    try:
        post_data = json.loads(result.stdout)
        return {
            "topic": topic,
            "accroche": post_data.get("accroche", ""),
            "points": post_data.get("points", []),
            "cta": post_data.get("cta", ""),
            "hashtags": post_data.get("hashtags", []),
            "full_content": f"{post_data['accroche']}\n\n{' '.join(post_data['points'])}\n\n{post_data['cta']}\n\n{' '.join(post_data['hashtags'])}"
        }
    except json.JSONDecodeError as e:
        return {
            "topic": topic,
            "error_message": f"Erreur parsing JSON M2: {e}",
            "accroche": "",
            "points": [],
            "cta": "",
            "hashtags": []
        }


def save_to_db(post_data: dict):
    """Sauvegarde le post dans jarvis-master.db."""
    conn = get_db_connection()
    init_db(conn)
    
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    
    if "error_message" in post_data and post_data["error_message"]:
        cursor.execute("""
        INSERT INTO linkedin_posts (topic, content, generated_at, status, error_message)
        VALUES (?, ?, ?, 'error', ?)
        """, (post_data["topic"], "", now, post_data["error_message"]))
    else:
        cursor.execute("""
        INSERT INTO linkedin_posts (topic, content, generated_at, status)
        VALUES (?, ?, ?, 'generated')
        """, (post_data["topic"], post_data["full_content"], now))
    
    conn.commit()
    return conn


def get_last_post(conn):
    """Récupère le dernier post généré."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM linkedin_posts ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    if row:
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))
    return None


def publish_to_linkedin(post_data: dict, browser_url: str):
    """
    Publie le post via Chrome CDP (Playwright).
    
    ATTENTION: NE JAMAIS PUBLIER AUTOMATIQUEMENT SANS VALIDATION!
    Ce script génère l'action de publication mais doit être validé par Turbo.
    """
    try:
        from playwright.sync_api import sync_playwright
        
        with sync_playwright() as p:
            # Launch Chrome avec CDP
            browser = p.chromium.connect_over_cdp(browser_url)
            context = browser.new_context(
                user_data_dir="/home/turbo/.config/google-chrome-default",  # OU le profil réel
                viewport={"width": 1280, "height": 720}
            )
            page = context.new_page()
            
            # Navigate vers LinkedIn (requis pour l'auth)
            print("🔄 Navigation vers LinkedIn.com...")
            page.goto("https://www.linkedin.com", wait_until="networkidle", timeout=30000)
            
            # Login si nécessaire (à configurer selon le profil)
            # ... logique de login si besoin ...
            
            # Create new post
            print("📝 Création d'un nouveau post...")
            page.goto("https://www.linkedin.com/feed/", wait_until="networkidle")
            
            # Click on "What's on your mind?"
            try:
                post_button = page.wait_for_selector(
                    "text=Start a post", timeout=10000
                )
                post_button.click()
            except Exception as e:
                print(f"⚠️  Ne peut pas trouver le bouton de publication: {e}")
                return {"success": False, "error": "Bouton de publication introuvable"}
            
            # Type content (multi-line)
            content = post_data["full_content"]
            page.locator("textarea").fill(content)
            
            # Add hashtags if not already included in content
            for tag in post_data["hashtags"]:
                if tag not in content:
                    page.locator("textarea").type(f"\n{tag}")
            
            print("✅ Contenu rédigé")
            
            # IMPORTANT: STOP! NE PAS CLIQUER SUR "POST" AUTOMATICQUEMENT!
            print("⏸️  ARRÊT! Le post est prêt mais NON PUBLIÉ.")
            print("👉 Turbo, valide manuellement en cliquant sur 'Post'")
            
            # Take screenshot for review
            page.screenshot(path="/home/turbo/IA/Core/jarvis/scripts/linkedin_draft_YYYY-MM-DD_HHMMSS.png")
            
            browser.close()
            
            return {
                "success": True,
                "draft_saved": "/home/turbo/IA/Core/jarvis/scripts/linkedin_draft.png",
                "warning": "ARRÊT! Post non publié. Valide manuellement sur LinkedIn."
            }
            
    except Exception as e:
        return {
            "success": False,
            "error": f"Erreur publication: {str(e)}"
        }


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="LinkedIn Publisher — Génère et publie posts LinkedIn")
    parser.add_argument("--topic", type=str, help="Sujet du post (ex: LLMs locaux)")
    parser.add_argument("--draft", action="store_true", help="Génère mais ne publie pas")
    parser.add_argument("--publish", action="store_true", help="Génère et tente publication")
    parser.add_argument("--status", action="store_true", help="Status du dernier post")
    
    args = parser.parse_args()
    
    if args.status:
        conn = get_db_connection()
        init_db(conn)
        post = get_last_post(conn)
        
        if post:
            print(f"📊 Dernier post généré:")
            print(f"  Topic: {post['topic']}")
            print(f"  Generated: {post['generated_at']}")
            print(f"  Status: {post['status']}")
            if post.get('error_message'):
                print(f"  Error: {post['error_message']}")
        else:
            print("⚠️  Aucun post généré encore")
        
        conn.close()
        return
    
    if not args.topic and not args.draft and not args.publish:
        parser.print_help()
        sys.exit(1)
    
    # Générer le post
    topic = args.topic or (input("Sujet du post LinkedIn : ") if args.publish else "LLMs locaux")
    print(f"🎯 Génération post LinkedIn — Sujet: {topic}")
    
    post_data = generate_post(topic)
    
    if "error_message" in post_data:
        print(f"❌ Erreur génération M2: {post_data['error_message']}")
        save_to_db(post_data)
        sys.exit(1)
    
    print("✅ Post généré !")
    print("\n--- CONTENU DU POST ---")
    print(post_data["full_content"])
    print("-----------------------\n")
    
    # Sauvegarder dans DB
    save_to_db(post_data)
    
    if args.draft:
        print("✅ Post sauvegardé en mode brouillon (pas de publication)")
        print("👉 Vérifie le contenu ci-dessus avant publication")
    
    elif args.publish:
        # IMPORTANT: NE PUBLIER QUE SI VALIDÉ PAR TURBO!
        print("\n⚠️  MODE PUBLICATION — ARRÊT POUR VALIDATION!")
        print("=" * 50)
        print("👉 Turbo, vérifie le post ci-dessus")
        print("   Si OK → relance avec: python linkedin-publisher.py --publish --topic 'Sujet'")
        print("   Ce script générera l'ébauche dans Chrome LinkedIn")
        print("=" * 50)
        
        input("\n👉 Appuie sur ENTRÉE pour continuer la publication (OU Ctrl+C pour annuler)")
        
        # Logique de validation — à implémenter selon workflow
        confirmed = input("✅ Confirmer la publication? (o/n): ")
        if confirmed.lower() == "o":
            result = publish_to_linkedin(post_data, BROWSER_CDP_URL)
            
            if result["success"]:
                now = datetime.now().isoformat()
                conn = get_db_connection()
                init_db(conn)
                cursor = conn.cursor()
                cursor.execute("""
                UPDATE linkedin_posts 
                SET published = TRUE, status = 'published', generated_at = ?
                WHERE topic = ?
                """, (now, topic))
                conn.commit()
                print("✅ Publication lancée! Vérifie LinkedIn pour confirmation.")
            else:
                print(f"❌ Erreur publication: {result['error']}")
        else:
            print("🚫 Publication annulée — post non publié")
    
    # Update DB with final status
    conn = get_db_connection()
    init_db(conn)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE linkedin_posts 
    SET status = CASE 
        WHEN published THEN 'published'
        WHEN "error_message" IN content THEN 'error'
        ELSE 'draft'
    END
    WHERE topic = ?
    """, (topic,))
    conn.commit()
    
    print("\n✅ Workflow terminé!")


if __name__ == "__main__":
    main()
