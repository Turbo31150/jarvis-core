#!/usr/bin/env python3
"""
Réduit les skills injectés par agent à ceux qui sont pertinents.
Copie les skills en trop dans un dossier archive.
"""
import os, shutil, glob

AGENTS_DIR = os.path.expanduser("~/.openclaw/agents")

# Skills pertinents par agent (max 5)
AGENT_SKILLS = {
    "main": ["coding-agent", "gemini"],
    "master": ["coding-agent"],
    "sys-ops": ["cowork-system.md", "cowork-linux.md"],
    "trading-engine": ["cowork-ai.md"],
    "monitoring": [],  # pas de skills
    "comms": [],
    "cluster-mgr": ["cowork-cluster.md"],
    "automation": ["cowork-automation.md"],
}

# Stats
trimmed = 0
for agent_name, keep_skills in AGENT_SKILLS.items():
    skills_dir = f"{AGENTS_DIR}/{agent_name}/agent/skills"
    if not os.path.exists(skills_dir):
        continue
    
    archive_dir = f"{AGENTS_DIR}/{agent_name}/agent/skills_archive"
    current = [f for f in os.listdir(skills_dir) if f.endswith('.md')]
    
    for skill_file in current:
        if not any(k in skill_file for k in (keep_skills or ['KEEP_NONE_999'])):
            src = f"{skills_dir}/{skill_file}"
            os.makedirs(archive_dir, exist_ok=True)
            dst = f"{archive_dir}/{skill_file}"
            shutil.move(src, dst)
            trimmed += 1
    
    remaining = len([f for f in os.listdir(skills_dir) if f.endswith('.md')])
    archived = len([f for f in os.listdir(archive_dir) if f.endswith('.md')]) if os.path.exists(archive_dir) else 0
    if archived > 0:
        print(f"  {agent_name}: {remaining} skills restants, {archived} archivés")

print(f"\nTotal archivé: {trimmed} skills")
print("Note: les skills archivés sont dans skills_archive/ — récupérables si besoin")
