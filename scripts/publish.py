#!/usr/bin/env python3
"""JARVIS Publisher — Post to LinkedIn/GitHub via BrowserOS CLI."""
import json, time, subprocess, sys, os, sqlite3, re, base64, argparse
from pathlib import Path

CLI = os.path.expanduser("~/.browseros/bin/browseros-cli")
DB = Path("/home/turbo/IA/Core/jarvis/data/jarvis-master.db")

def cli(*args):
    return subprocess.run([CLI]+list(args), capture_output=True, text=True, timeout=30).stdout.strip()

def find_page(kw):
    for line in cli("pages").split("\n"):
        if kw.lower() in line.lower():
            m = re.search(r"^\s*(\d+)\.", line)
            if m: return int(m.group(1))
    return None

def snap_find(page, keyword):
    for line in cli("snap","-p",str(page)).split("\n"):
        if keyword.lower() in line.lower():
            m = re.search(r"\[(\d+)\]", line)
            if m: return m.group(1)
    return None

def save_db(action, target, content):
    try:
        conn = sqlite3.connect(str(DB))
        conn.execute("INSERT INTO linkedin_actions (action_type,target_person,content,timestamp) VALUES (?,?,?,?)",
            (action, target, content[:500], time.strftime("%Y-%m-%dT%H:%M:%S")))
        conn.commit(); conn.close()
    except: pass

def publish_linkedin(text):
    """Post on LinkedIn via BrowserOS CLI."""
    page = find_page("linkedin")
    if not page:
        cli("open", "https://www.linkedin.com/feed/")
        time.sleep(5)
        page = find_page("linkedin")
    if not page:
        print("ERROR: No LinkedIn tab"); return False

    cli("nav", "https://www.linkedin.com/feed/", "-p", str(page))
    time.sleep(4)

    # Click "Commencer un post"
    trigger = snap_find(page, "commencer") or snap_find(page, "post") or snap_find(page, "share")
    if trigger:
        cli("click", trigger, "-p", str(page))
        time.sleep(3)
        editor = snap_find(page, "textbox") or snap_find(page, "editor")
        if editor:
            cli("fill", editor, text, "-p", str(page))
            time.sleep(1)
            pub = snap_find(page, "publier")
            if pub:
                cli("click", pub, "-p", str(page))
                print("POSTED on LinkedIn!")
                save_db("post", "self", text[:200])
                return True
    print("Could not post — check BrowserOS manually")
    return False

def publish_github(content, repo="Turbo31150/Turbo31150"):
    """Update GitHub README."""
    sha = subprocess.run(["gh","api",f"repos/{repo}/contents/README.md","--jq",".sha"],
        capture_output=True,text=True).stdout.strip()
    encoded = base64.b64encode(content.encode()).decode()
    data = json.dumps({"message":"Update README via JARVIS","content":encoded,"sha":sha})
    r = subprocess.run(["gh","api",f"repos/{repo}/contents/README.md","-X","PUT","--input","-"],
        input=data, capture_output=True, text=True)
    if "content" in r.stdout:
        print("PUSHED to GitHub!")
        save_db("github_push", repo, "README updated")
        return True
    print(f"ERROR: {r.stderr[:80]}"); return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JARVIS Publisher")
    parser.add_argument("platform", choices=["linkedin","github"])
    parser.add_argument("--text", help="Text to publish")
    parser.add_argument("--file", help="Read from file")
    args = parser.parse_args()

    content = Path(args.file).read_text() if args.file else (args.text or sys.stdin.read())

    if args.platform == "linkedin": publish_linkedin(content)
    elif args.platform == "github": publish_github(content)
