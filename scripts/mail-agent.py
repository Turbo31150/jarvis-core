#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mail-agent.py — Agent de gestion emails IMAP pour prospection IA

Fonctions :
- fetch_unread() : liste les nouveaux emails non lus
- classify_email(subject, body) : classe via qwen3.5-9b (INBOX, PROSPECTION, INFO, SPAM)
- auto_reply(email) : génère réponse automatique pour prospection IA

Cron : */10 * * * * pour vérifier les nouveaux mails

Usage:
    python mail-agent.py --status           # Status connexions
    python mail-agent.py --inbox            # Liste emails non lus
    python mail-agent.py --classify-all     # Classe tous les nouveaux emails
    python mail-agent.py --reply-all        # Génère réponses (validation requise!)
    python mail-agent.py --prospection      # Filter only prospection emails
"""

import os
import sys
import json
import sqlite3
import ssl
import imaplib
from datetime import datetime
from email.header import decode_header
from pathlib import Path

# Chemins
ENV_PATH = "/home/turbo/Workspaces/jarvis-linux/.env"
DB_PATH = "/home/turbo/jarvis-master.db"
SCRIPT_DIR = Path(__file__).parent
MODEL_M2 = "deepseek-coder-v2-lite-instruct"


def load_env_vars():
    """Charge les variables IMAP depuis .env"""
    env_vars = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip().strip('"\'')
    return env_vars


def get_db_connection():
    """Connexion SQLite"""
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Base de données non trouvée: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    return conn


def init_db(conn):
    """Initialise les tables si absentes"""
    cursor = conn.cursor()
    
    # Table emails_importés (historique)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS emails_imported (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT NOT NULL,
        subject TEXT NOT NULL,
        from_email TEXT,
        sender_name TEXT,
        date_sent TEXT,
        body_preview TEXT,
        importance INTEGER DEFAULT 0,
        classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_read BOOLEAN DEFAULT FALSE
    )""")
    
    # Table classification_results (détails classification)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS classification_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email_id INTEGER,
        category TEXT,
        confidence FLOAT,
        model_used TEXT DEFAULT 'qwen3.5-9b-a3b',
        analysis TEXT,
        action_suggested TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    
    # Table auto_replies (historique réponses générées)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS auto_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        to_email TEXT,
        reply_subject TEXT,
        reply_body TEXT,
        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'pending',
        sent BOOLEAN DEFAULT FALSE
    )""")
    
    conn.commit()


def decode_mime_header(header):
    """Décode l'entête email"""
    if isinstance(header, list):
        return decode_header(header[0])[0]
    return header


def fetch_unread_emails():
    """Fetch les emails non lus depuis les comptes IMAP"""
    env_vars = load_env_vars()
    
    print("📬 Connexion aux comptes IMAP...")
    accounts = []
    
    # Compte 1 (mining/prospection)
    if all(k in env_vars for k in ['IMAP_HOST_1', 'IMAP_USER_1', 'IMAP_PASS_1']):
        try:
            server = ssl.wrap_socket(imaplib.IMAP4(env_vars['IMAP_HOST_1'], env_vars['IMAP_PORT_1'] or 993))
            server.login(env_vars['IMAP_USER_1'], env_vars['IMAP_PASS_1'])
            account_id = len(accounts) + 1
            accounts.append({
                'id': account_id,
                'host': env_vars['IMAP_HOST_1'],
                'user': env_vars['IMAP_USER_1'],
                'connection': server
            })
            print(f"✅ Compte {account_id}: {env_vars['IMAP_USER_1']} connecté")
        except Exception as e:
            print(f"❌ Erreur compte {account_id}: {str(e)}")
    
    # Compte 2 (perso)
    if all(k in env_vars for k in ['IMAP_HOST_2', 'IMAP_USER_2', 'IMAP_PASS_2']):
        try:
            server = ssl.wrap_socket(imaplib.IMAP4(env_vars['IMAP_HOST_2'], env_vars['IMAP_PORT_2'] or 993))
            server.login(env_vars['IMAP_USER_2'], env_vars['IMAP_PASS_2'])
            account_id = len(accounts) + 1
            accounts.append({
                'id': account_id,
                'host': env_vars['IMAP_HOST_2'],
                'user': env_vars['IMAP_USER_2'],
                'connection': server
            })
            print(f"✅ Compte {account_id}: {env_vars['IMAP_USER_2']} connecté")
        except Exception as e:
            print(f"❌ Erreur compte {account_id}: {str(e)}")
    
    if not accounts:
        print("⚠️  Aucun compte IMAP configuré. Voir ~/.openclaw/docs/imap-setup.md")
        return []
    
    # Search for UNSEEN messages
    all_unseen = []
    for acc in accounts:
        try:
            acc['connection'].select('INBOX')
            status, data = acc['connection'].search(None, 'UNSEEN')
            
            if status == 'OK' and data:
                msg_ids = data[0].split()
                print(f"📬 Compte {acc['id']}: {len(msg_ids)} emails non lus trouvés")
                
                for msg_id in msg_ids[:10]:  # Limit to first 10 for performance
                    status, msg_data = acc['connection'].fetch(msg_id, '(RFC822)')
                    
                    if status == 'OK' and msg_data:
                        raw_email = msg_data[0]
                        email_parser = email_message_from_raw(raw_email)
                        
                        all_unseen.append({
                            'account_id': acc['id'],
                            'subject': decode_mime_header(email_parser.subject),
                            'from': f"{decode_mime_header(email_parser.get('From', ''))}" if email_parser.get('From') else '',
                            'date': email_parser.get('Date', ''),
                            'body_preview': get_email_preview(email_parser)
                        })
                        
                        # Mark as read on server
                        acc['connection'].store(msg_id, '+FLAGS', '\\Seen')
            
        except Exception as e:
            print(f"❌ Erreur lecture emails compte {acc['id']}: {str(e)}")
    
    return all_unseen


def email_message_from_raw(raw_bytes):
    """Parse raw IMAP email into Message-like object"""
    from email.message import EmailMessage
    from email.parser import BytesParser
    
    message = BytesParser().parsebytes(raw_bytes)
    
    # Create EmailMessage for compatibility
    email_msg = EmailMessage()
    email_msg['Subject'] = message.get('Subject', '')
    email_msg['From'] = message.get('From', '')
    email_msg['To'] = message.get('To', '')
    email_msg['Date'] = message.get('Date', '')
    
    # Get body (first part)
    parts = message.get_payload(keep_alternatives=False, decode=True)
    if isinstance(parts, list):
        email_msg.set_payload(parts[0])
    else:
        email_msg.set_payload(parts)
    
    return email_msg


def get_email_preview(email_msg, max_chars=500):
    """Get preview of email body"""
    payload = email_msg.get_payload()
    if isinstance(payload, list):
        payload = ''.join(str(p) for p in payload)
    else:
        payload = str(payload)
    
    # Clean up attachments markers
    preview = payload.replace("=ATTACHMENT=", " ").replace("=" * 50, "")[:max_chars]
    return preview.strip()


def classify_email(subject: str, body_preview: str) -> dict:
    """
    Classifie un email via M2/qwen3.5-9b
    
    Catégories :
    - INBOX : Email important à traiter manuellement
    - PROSPECTION : Potentiel lead (freelance, partnership)
    - INFO : Newsletter, update technique
    - SPAM / IGNORE : Junk mail
    """
    
    prompt = f"""Tu es un expert en tri d'emails pour une prospection IA/freelance.

Analyse ce email:

SUJET: {subject}
CONTENU (prévisualisation): {body_preview}

Catégorise dans l'une de ces catégories :
1. INBOX — Email important à lire et répondre manuellement (client existant, offre collaboration)
2. PROSPECTION — Potentiel lead freelance/partnership (proposition claire, budget mentionné)
3. INFO — Newsletter, update technique, contenu passif (penses seulement intéressante)
4. SPAM/IGNORE — Spam, phishing, junk mail

Critères :
- Budget ou TJM mentionné → PROSPECTION si montant > 300€
- Demande de service IA spécifique → INBOX
- Newsletter "Nous sommes dans le top..." → INFO
- Malformé / Link soupçon → SPAM/IGNORE

Réponds UNIQUEMENT avec ce format JSON:
{{
  "category": "INBOX|PROSPECTION|INFO|SPAM",
  "confidence": 0.85,
  "analysis": "...",
  "action_suggested": "lire et répondre | générer réponse automatique | archive | ignorer"
}}
"""
    
    try:
        # Use exec to call M2 via curl.exe (PowerShell on Windows)
        result = subprocess.run(
            ["exec", "-c", f'curl.exe -s http://192.168.1.26:1234/v1/chat/completions ^\\-H "Content-Type: application/json" ^\\-H "Authorization: Bearer LMSTUDIO_KEY_M2_REDACTED" ^\\-d "{{\\"model\\":\\"{MODEL_M2}\\",\\"messages\\":[{{\\"role\\":\\"user\\",\\"content\\":\\"{prompt}\\"}}],\\"max_tokens\\":1024}}"'],
            capture_output=True, 
            text=True
        )
        
        try:
            classification = json.loads(result.stdout)
            
            # Log to DB
            conn = get_db_connection()
            init_db(conn)
            cursor = conn.cursor()
            
            now = datetime.now().isoformat()
            
            # Insert into emails_imported (if not already imported)
            cursor.execute("""
            INSERT OR IGNORE INTO emails_imported 
            (subject, from_email, date_sent, body_preview)
            VALUES (?, ?, ?, ?)
            """, (subject, email_msg.get('From', ''), '', body_preview[:200]))
            
            # Insert classification result
            cursor.execute("""
            INSERT INTO classification_results 
            (email_id, category, confidence, analysis, action_suggested, model_used)
            VALUES ((SELECT MAX(id) FROM emails_imported WHERE subject = ?), ?, ?, ?, ?, 'qwen3.5-9b-a3b')
            """, (subject, classification.get('category', ''), classification.get('confidence', 0), 
                  classification.get('analysis', ''), classification.get('action_suggested', '')))
            
            conn.commit()
            
            return {
                "success": True,
                "classification": classification
            }
        
        except json.JSONDecodeError as e:
            print(f"⚠️  Erreur parsing JSON classification: {e}")
            return {
                "success": False,
                "error": f"Erreur parsing M2 response: {e}",
                "raw_output": result.stdout[:500]
            }
            
    except Exception as e:
        print(f"❌ Erreur classification email: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }


def auto_reply(email_info: dict) -> dict:
    """
    Génère une réponse automatique pour emails de prospection
    
    ATTENTION: NE JAMAIS ENVOYER AUTOMATICQUEMENT SANS VALIDATION!
    Ce script génère la réponse, Turbo valide et envoie manuellement.
    """
    
    to_email = email_info['from']  # Reply to the sender
    subject = email_info['subject']
    body_preview = email_info.get('body_preview', '')
    
    prompt = f"""Tu es Franc Delmas (Turbo), Ingénieur IA & Freelance, spécialisé :
- LLMs locaux sur cluster multi-GPU (6 GPUs)
- Automatisation Python avec JARVIS OS
- Trading algorithmique MEXC API
- Prospection freelance Codeur.com (TJM 400-600€)

Contexte: Email de {to_email} sur le sujet "{subject}"

Génère une réponse professionnelle mais concise adaptée au contexte.

Format JSON EXACT:
{{
  "reply_subject": "...",
  "reply_body": "...",
  "tone": "professional|friendly|technical"
}}

Instructions :
- Si PROSPECTION : remercie, présente JARVIS OS + expertise IA, invite à DM/codeur.com
- Si CLIENT EXISTANT : demande feedback/résultats
- Si SPAM/IGNORE : ignorer (reply_subject: null)
"""
    
    try:
        # Use M2 for classification-based response
        result = subprocess.run(
            ["exec", "-c", f'curl.exe -s http://192.168.1.26:1234/v1/chat/completions ^\\-H "Content-Type: application/json" ^\\-H "Authorization: Bearer LMSTUDIO_KEY_M2_REDACTED" ^\\-d "{{\\"model\\":\\"{MODEL_M2}\\",\\"messages\\":[{{\\"role\\":\\"user\\",\\"content\\":\\"{prompt}\\"}}],\\"max_tokens\\":500}}"'],
            capture_output=True, 
            text=True
        )
        
        try:
            response_data = json.loads(result.stdout)
            
            # Save to auto_replies table
            conn = get_db_connection()
            init_db(conn)
            cursor = conn.cursor()
            
            now = datetime.now().isoformat()
            cursor.execute("""
            INSERT INTO auto_replies (to_email, reply_subject, reply_body, generated_at)
            VALUES (?, ?, ?, ?)
            """, (to_email, response_data.get('reply_subject', ''), 
                  response_data.get('reply_body', ''), now))
            
            conn.commit()
            
            return {
                "success": True,
                "response": response_data
            }
        
        except json.JSONDecodeError as e:
            print(f"⚠️  Erreur parsing JSON réponse: {e}")
            return {
                "success": False,
                "error": f"Erreur parsing M2 response: {e}",
                "raw_output": result.stdout[:500]
            }
            
    except Exception as e:
        print(f"❌ Erreur génération réponse: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }


def display_emails(emails: list):
    """Affiche les emails de manière lisible"""
    for i, email in enumerate(emails, 1):
        print(f"\n{'='*60}")
        print(f"[{i}] 📧 {email['subject'][:60]}...")
        print(f"   De: {email['from'][:50] if email['from'] else 'Inconnu'}")
        print(f"   Compte: #{email['account_id']}")
        print(f"   Date: {email.get('date', 'N/A')}")
        if email.get('body_preview'):
            preview = email['body_preview'].replace('\n', ' ').strip()[:300]
            print(f"   Prévisualisation: {preview}...")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Mail Agent — Gestion emails IMAP pour prospection IA")
    parser.add_argument("--status", action="store_true", help="Status connexions IMAP")
    parser.add_argument("--inbox", action="store_true", help="Liste emails non lus")
    parser.add_argument("--classify-all", action="store_true", help="Classifie tous les nouveaux emails")
    parser.add_argument("--reply-all", action="store_true", help="Génère réponses (VALIDATION requise!)")
    parser.add_argument("--prospection", action="store_true", help="Filter only PROSPECTION emails")
    
    args = parser.parse_args()
    
    # Initialize DB
    conn = get_db_connection()
    init_db(conn)
    
    # Get unread emails
    print("📬 Récupération emails non lus...")
    emails = fetch_unread_emails()
    
    if not emails:
        print("⚠️  Aucun email non lu trouvé.")
        conn.close()
        return
    
    print(f"\n📬 {len(emails)} emails non lus récupérés")
    
    # Display all emails
    display_emails(emails)
    
    # Classification
    if args.classify_all or args.prospection:
        print("\n🤖 Classification en cours...")
        
        for email in emails:
            try:
                classification = classify_email(email['subject'], email.get('body_preview', ''))
                
                if classification.get("success"):
                    cat = classification["classification"].get("category", "UNKNOWN")
                    print(f"\n✅ [{cat}] {email['subject'][:50]}...")
                    
                    if args.prospection and cat != "PROSPECTION":
                        print(f"   → Ignoré (non PROSPECTION)")
                    else:
                        cursor = conn.cursor()
                        cursor.execute("""
                        UPDATE emails_imported 
                        SET is_read = TRUE 
                        WHERE subject = ?
                        """, (email['subject'],))
                        conn.commit()
                
                elif "error_message" in classification:
                    print(f"\n⚠️  Erreur classification: {classification['error_message'][:100]}...")
            
            except Exception as e:
                print(f"\n❌ Erreur classifcation email {email['subject'][:30]}: {str(e)}")
        
        conn.close()
        return
    
    # Auto-reply generation
    if args.reply_all:
        print("\n✉️  Génération réponses automatiques (VALIDATION REQUISE!)")
        print("="*50)
        
        for email in emails:
            try:
                response = auto_reply(email)
                
                if response.get("success"):
                    reply_data = response["response"]
                    print(f"\n✅ {email['subject'][:50]}...")
                    print(f"   Objet réponse: {reply_data.get('reply_subject', 'N/A')[:50]}")
                    print(f"   ---")
                    print(reply_data.get('reply_body', '')[:300])
                    
                elif "error_message" in response:
                    print(f"\n⚠️  Erreur génération: {response['error_message'][:100]}...")
            
            except Exception as e:
                print(f"\n❌ Erreur réponse email {email['subject'][:30]}: {str(e)}")
        
        print("\n" + "="*50)
        print("⚠️  IMPORTANT: Ces réponses doivent être validées par Turbo avant envoi!")
        print("   Relance avec : python mail-agent.py --send (après validation manuelle)")
    
    conn.close()


if __name__ == "__main__":
    main()
