#!/usr/bin/env python3
"""Gmail IMAP multi-comptes — tri, notifications Telegram, réponses suggérées"""
import imaplib, email, json, re, sqlite3, requests
from email.header import decode_header
from datetime import datetime

TELEGRAM_TOKEN = "8369376863:AAF-7YGDbun8mXWwqYJFj-eX6P78DeIu9Aw"
CHAT_ID = "2010747443"
DB_PATH = "/home/turbo/Workspaces/jarvis-linux/data/db/etoile.db"

LABELS = {
    "codeur":    ["codeur.com", "confirm.cod"],
    "linkedin":  ["linkedin.com"],
    "client":    ["devis", "projet", "mission", "freelance", "contrat", "proposition"],
    "urgent":    ["urgent", "asap", "immédiat", "rapidement"],
    "facture":   ["facture", "invoice", "paiement", "virement"],
    "trading":   ["kucoin", "binance", "coinex", "bingx", "mexc", "liquidation", "futures", "mining"],
    "newsletter":["unsubscribe", "désabonner", "newsletter"],
}
REPLY_SIGNALS = ["vous contacte", "disponible", "intéressé", "pouvez-vous",
                 "offre acceptée", "retenu", "sélectionné", "votre profil", "collaboration"]

def decode_str(s):
    if not s: return ""
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)

def classify(subject, sender, body):
    text = (subject + " " + sender + " " + body[:200]).lower()
    for label, keywords in LABELS.items():
        if any(kw in text for kw in keywords):
            return label
    return "inbox"

def needs_reply(subject, body):
    return any(s in (subject + " " + body[:300]).lower() for s in REPLY_SIGNALS)

def get_body(msg):
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    return part.get_payload(decode=True).decode("utf-8", errors="replace")[:500]
        else:
            return msg.get_payload(decode=True).decode("utf-8", errors="replace")[:500]
    except: return ""
    return ""

def gen_reply(subject, body, sender):
    prompt = (f"Tu es Franck Delmas, développeur IA freelance.\n"
              f"Email de {sender}:\nObjet: {subject}\n{body[:300]}\n\n"
              f"Réponse professionnelle courte (60 mots max).\n"
              f"Signe: Franck Delmas — Développeur IA & Automatisation")
    try:
        r = requests.post("http://127.0.0.1:11434/api/generate",
                          json={"model":"gemma3:4b","prompt":prompt,"stream":False}, timeout=30)
        return r.json().get("response","").strip()
    except: return ""

def notify(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML"}, timeout=5)
    except: pass

def save_to_db(emails_data):
    """Sauvegarder les emails analysés dans etoile.db"""
    try:
        db = sqlite3.connect(DB_PATH)
        db.execute("""CREATE TABLE IF NOT EXISTS mail_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            account TEXT, subject TEXT, sender TEXT,
            label TEXT, reply_needed INTEGER, body_preview TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS mail_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            account TEXT, unread_count INTEGER, total_count INTEGER
        )""")
        for item in emails_data:
            db.execute(
                "INSERT INTO mail_log (account,subject,sender,label,reply_needed,body_preview) VALUES (?,?,?,?,?,?)",
                (item["account"], item["subject"][:100], item["sender"][:80],
                 item["label"], int(item["reply_needed"]), item["body"][:200])
            )
        db.commit()
        db.close()
        print(f"  → {len(emails_data)} emails sauvegardés en SQL")
    except Exception as e:
        print(f"  [SQL ERR] {e}")

def check_account(acc):
    email_addr = acc["email"]
    app_pw = acc["app_password"]
    items = []
    unread_total = 0

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(email_addr, app_pw)
        mail.select("INBOX")
        _, msgs = mail.search(None, "UNSEEN")
        all_unread = msgs[0].split()
        unread_total = len(all_unread)
        recent = all_unread[-20:]  # 20 derniers

        for uid in recent:
            try:
                _, data = mail.fetch(uid, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])
                subject = decode_str(msg.get("Subject",""))
                sender  = decode_str(msg.get("From",""))
                body    = get_body(msg)
                label   = classify(subject, sender, body)
                reply   = needs_reply(subject, body)
                items.append({"account":email_addr,"subject":subject,"sender":sender,
                              "label":label,"reply_needed":reply,"body":body[:200]})
            except: continue

        mail.close()
        mail.logout()

        # Sauvegarder stats
        try:
            db = sqlite3.connect(DB_PATH)
            db.execute("""CREATE TABLE IF NOT EXISTS mail_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                account TEXT, unread_count INTEGER, total_count INTEGER)""")
            db.execute("INSERT INTO mail_stats (account,unread_count,total_count) VALUES (?,?,?)",
                       (email_addr, unread_total, 0))
            db.commit()
            db.close()
        except: pass

    except Exception as e:
        print(f"❌ {email_addr}: {e}")

    return items, unread_total

def main():
    with open("/home/turbo/.openclaw/workspace/dev/email_config.json") as f:
        cfg = json.load(f)

    accounts = cfg.get("accounts", [])
    all_items = []
    all_unread = {}

    for acc in accounts:
        print(f"\n📧 {acc['email']}")
        items, unread = check_account(acc)
        all_items.extend(items)
        all_unread[acc["email"]] = unread
        print(f"   {unread} non lus | {len(items)} analysés")

    # Sauvegarde SQL
    save_to_db(all_items)

    # Résumé Telegram
    icons = {"codeur":"📋","linkedin":"💼","client":"🔴","urgent":"🚨",
             "facture":"💶","trading":"📈","newsletter":"📰","inbox":"📩"}

    by_label = {}
    for m in all_items:
        by_label.setdefault(m["label"], []).append(m)

    summary = "📬 <b>Résumé mails</b>\n"
    for acc_email, count in all_unread.items():
        summary += f"  {acc_email}: <b>{count}</b> non lus\n"
    summary += "\n"

    for label, items in sorted(by_label.items()):
        if label in ("newsletter","trading"): continue
        icon = icons.get(label, "📩")
        summary += f"{icon} <b>{label.upper()}</b> ({len(items)})\n"
        for m in items[:2]:
            acct = "franck" if "franck" in m["account"] else "mining"
            summary += f"  [{acct}] {m['subject'][:42]}\n"
    notify(summary)

    # Réponses suggérées
    for m in all_items:
        if m["reply_needed"] and m["label"] in ("client","codeur","urgent"):
            reply_text = gen_reply(m["subject"], m["body"], m["sender"])
            if reply_text:
                acct = "franck" if "franck" in m["account"] else "mining"
                notify(f"💬 <b>Réponse suggérée [{acct}]</b>\n"
                       f"<i>{m['subject'][:50]}</i>\n\n{reply_text[:500]}")

    print(f"\n✅ Total: {sum(all_unread.values())} non lus | "
          f"{len(all_items)} analysés | SQL sauvegardé")

if __name__ == "__main__":
    main()
