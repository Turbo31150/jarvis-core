"""JARVIS BrowserOS Workflows — generate and validate tab configs."""
import json, subprocess, os
from pathlib import Path

CLI = os.path.expanduser("~/.browseros/bin/browseros-cli")
SKILLS_DIR = Path.home() / ".browseros/skills"
MEMORY_FILE = Path.home() / ".browseros/memory/CORE.md"

REQUIRED_TABS = {
    "FREELANCE": [
        ("LinkedIn Feed", "https://www.linkedin.com/feed/"),
        ("Codeur Profil", "https://www.codeur.com/-6666zlkh"),
        ("Codeur Projets", "https://www.codeur.com/projects"),
    ],
    "IA WEB": [
        ("ChatGPT", "https://chatgpt.com/"),
        ("Claude", "https://claude.ai/new"),
        ("Gemini", "https://gemini.google.com/app"),
        ("Perplexity", "https://www.perplexity.ai/"),
        ("AI Studio", "https://aistudio.google.com/prompts/new_chat"),
    ],
    "PORTFOLIO": [
        ("GitHub", "https://github.com/Turbo31150"),
    ],
}

COLORS = {"FREELANCE": "blue", "IA WEB": "green", "PORTFOLIO": "yellow"}

def _cli(*args):
    r = subprocess.run([CLI]+list(args), capture_output=True, text=True, timeout=15)
    return r.stdout.strip()

def validate_tabs():
    pages = _cli("pages")
    missing = {}
    for group, tabs in REQUIRED_TABS.items():
        for name, url in tabs:
            domain = url.split('/')[2]
            if domain not in pages:
                missing.setdefault(group, []).append((name, url))
    return missing

def ensure_tabs():
    missing = validate_tabs()
    opened = []
    for group, tabs in missing.items():
        for name, url in tabs:
            _cli("open", url)
            opened.append(f"{group}: {name}")
    return opened

def ensure_groups():
    import re
    pages = _cli("pages")
    for group, color in COLORS.items():
        ids = []
        for line in pages.split('\n'):
            for _, url in REQUIRED_TABS.get(group, []):
                domain = url.split('/')[2]
                if domain in line:
                    m = re.search(r'^\s*(\d+)\.', line)
                    if m: ids.append(m.group(1))
        if ids:
            _cli("group", "create", "--title", group, *ids)

def list_skills():
    skills = []
    for d in SKILLS_DIR.iterdir():
        if d.is_dir():
            skill_file = d / "SKILL.md"
            if skill_file.exists():
                skills.append(d.name)
    return skills

def morning_startup():
    ensure_tabs()
    ensure_groups()
    return {"tabs_checked": True, "groups_set": True, "skills": list_skills()}

def crash_recovery():
    return ensure_tabs()

if __name__ == "__main__":
    print(f"Skills: {list_skills()}")
    missing = validate_tabs()
    print(f"Missing tabs: {missing if missing else 'None'}")
