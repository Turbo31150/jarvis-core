#!/usr/bin/env python3
"""Feedback loop VAGUE: surveille completion et génère vague suivante si résultats bons."""
import sqlite3, time, subprocess, json
from pathlib import Path
from datetime import datetime

DB  = Path.home() / 'IA/Core/jarvis/data/jarvis-master.db'
LOG = Path.home() / 'IA/Core/jarvis/logs/oc-feedback.log'

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open(LOG, 'a') as f: f.write(line + '\n')

def get_stats(prefix):
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        f"SELECT status, COUNT(*) FROM openclaw_tasks WHERE description LIKE '{prefix}%' GROUP BY status"
    ).fetchall()
    conn.close()
    return dict(rows)

def generate_next_vague(current_num, stats):
    """Génère vague N+1 basée sur résultats vague N."""
    next_num = current_num + 1
    conn = sqlite3.connect(str(DB))
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    failed = conn.execute(
        f"SELECT description, agent FROM openclaw_tasks WHERE description LIKE '[OC{current_num}%' AND status='failed' LIMIT 15"
    ).fetchall()
    completed = stats.get('completed', 0)
    total = sum(stats.values())
    success_rate = (completed / total * 100) if total else 0
    
    tasks = []
    # Retry tâches échouées avec simplification
    for desc, agent in failed[:8]:
        tid = desc.split(']')[0][1:] if ']' in desc else 'X'
        tasks.append((f"[OC{next_num}-RETRY-{tid}] Reprendre {desc[:80]}... — décomposer en 3 micro-étapes et exécuter", agent, 9))
    
    # Tâches de consolidation basées sur succès
    tasks += [
        (f"[OC{next_num}-AUDIT-01] Auditer résultats VAGUE-{current_num}: vérifier que les {completed} tâches réussies sont bien persistées et fonctionnelles", "ops-sre", 10),
        (f"[OC{next_num}-AUDIT-02] Tester bout en bout les modules améliorés en VAGUE-{current_num}: créer test_suite_{current_num}.py avec 1 test par amélioration", "cowork-testing", 9),
        (f"[OC{next_num}-OPT-01] Optimiser les {completed} implémentations réussies: profiler avec cProfile, identifier hotspots, réduire latence de 20%", "ai-engine", 8),
        (f"[OC{next_num}-DOC-01] Documenter toutes les nouvelles fonctionnalités VAGUE-{current_num}: README.md + exemples d'utilisation pour chaque", "cowork-docs", 7),
        (f"[OC{next_num}-NEXT-01] Planifier VAGUE-{next_num+1}: analyser gaps restants, identifier 20 nouvelles améliorations prioritaires, créer issues GitHub", "coordinator", 9),
    ]
    
    for desc, agent, prio in tasks:
        conn.execute(
            "INSERT INTO openclaw_tasks (description, agent, status, priority, created_at) VALUES (?, ?, 'pending', ?, ?)",
            (desc, agent, prio, now)
        )
    conn.commit()
    conn.close()
    log(f"VAGUE-{next_num}: {len(tasks)} tâches générées (succès VAGUE-{current_num}: {success_rate:.0f}%)")
    return len(tasks)

def main():
    current_vague = 3
    max_vague = 15  # générer jusqu'à VAGUE-12
    
    log(f"Feedback watcher démarré — surveille jusqu'à VAGUE-{max_vague}")
    
    while current_vague <= max_vague:
        prefix = f'[OC{current_vague}'
        log(f"Surveillance VAGUE-{current_vague}...")
        
        while True:
            stats = get_stats(prefix)
            total = sum(stats.values())
            pending = stats.get('pending', 0)
            completed = stats.get('completed', 0)
            failed = stats.get('failed', 0)
            
            if total == 0:
                log(f"VAGUE-{current_vague}: pas encore de tâches, attente...")
                time.sleep(60)
                continue
            
            log(f"VAGUE-{current_vague}: {completed} OK / {failed} KO / {pending} pending / {total} total")
            
            # Si plus de pending → vague terminée
            if pending == 0 and total > 0:
                log(f"VAGUE-{current_vague} TERMINÉE — taux succès: {completed/total*100:.0f}%")
                if current_vague < max_vague:
                    n = generate_next_vague(current_vague, stats)
                    log(f"→ VAGUE-{current_vague+1} générée avec {n} tâches")
                break
            
            time.sleep(120)
        
        current_vague += 1
    
    log(f"Toutes les vagues jusqu'à {max_vague} complètes — arrêt watcher")

if __name__ == '__main__':
    main()
