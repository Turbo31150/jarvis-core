# JARVIS-TURBO — Claude Code Project Configuration

## Identity

This is the JARVIS multi-agent orchestration system. The operator is **Turbo31150**, managing a GPU cluster (M1/M2/M3/OL1), a trading pipeline, browser automation (BrowserOS), and a full development stack.

Language: French preferred for communication, English for code.

---

## Auto-Activation Rules

### Session Startup Sequence

At every session start, automatically execute these CLIs:
```bash
# 1. Health snapshot (2s)
python3 /home/turbo/jarvis-linux/scripts/jarvis-monitor.py snapshot --json
# 2. Kill zombies (1s)
python3 /home/turbo/jarvis-linux/scripts/jarvis-zombie.py kill
# 3. GPU thermal check (1s)
python3 /home/turbo/jarvis-linux/scripts/jarvis-gpu.py thermal
# 4. Cluster health (2s)
python3 /home/turbo/jarvis-linux/scripts/jarvis-cluster.py health --json
# 5. Security quick check (2s)
python3 /home/turbo/jarvis-linux/scripts/jarvis-security.py ports
```

### CLI Auto-Trigger During Session

Detect keywords in user messages and auto-execute the matching CLI:

| Keywords detected | CLI to execute | Priority |
|-------------------|----------------|----------|
| status, health, system state | `jarvis status` | 10 |
| gpu, vram, thermal, temperature | `jarvis-gpu status` | 9 |
| zombie, defunct, dead process | `jarvis-zombie kill` | 9 |
| security, audit, ports, secrets | `jarvis-security scan` | 8 |
| cluster, node, m1, m2, m3, ol1 | `jarvis-cluster health` | 8 |
| dispatch, send, consensus | `jai [target] "[prompt]"` | 8 |
| boot, startup, layer | `jarvis-layers check` | 7 |
| monitor, watch, live | `jarvis-monitor snapshot` | 7 |
| predict, optimize, decision | `jarvis-decide predict` | 7 |
| codeur, mission, freelance | invoke skill codeur-operator | 6 |
| linkedin, post, content | invoke skill linkedin-operator | 6 |

### Available CLIs (all in /usr/local/bin/)

```
jarvis status|health|gpu|ask|security|clean|load|skills
jarvis-gpu status|load|unload|thermal|vram
jarvis-cluster health|nodes|route|failover
jarvis-security scan|ports|secrets|guard
jarvis-zombie list|kill|parents
jarvis-monitor snapshot|live|services
jarvis-layers check|boot
jarvis-decide predict|simulate|matrix|optimize|visualize
jai [23 targets] "[prompt]" --json
```

### Agent Auto-Trigger Matrix

Use these agents **proactively** when their conditions are met — do NOT wait for the user to ask:

| Agent | Auto-Trigger When |
|-------|-------------------|
| `omega-dev-agent` | New feature implementation, TDD, refactoring, code generation |
| `omega-analysis-agent` | Deep research, tech comparison, due diligence, code review |
| `omega-system-agent` | Infra changes, backups, automation workflows, monitoring rules |
| `omega-security-agent` | Security implementations, incident response, vulnerability scan |
| `omega-docs-agent` | Documentation requests, learning plans, knowledge management |
| `omega-trading-agent` | Market analysis, trading strategies, backtesting, portfolio management |
| `omega-voice-agent` | Voice commands, audio feedback, HUD alerts, speech-to-text |
| `jarvis-system-agent` | Any cluster operation, health check, MCP call, GPU management, EasySpeak, wave launch |
| `ia-flow` | CPU > 80%, task separation violations, backpressure needed, system health queries |
| `architect-guardian` | Structural changes to multi-agent system, performance anomalies, drift detection, pre-mutation validation |
| `task-decomposer-prime` | Multi-domain requests (code + security + deployment), complex multi-step tasks |
| `jarvis-auditor` | After deployments, code changes to critical paths, security-sensitive operations |
| `jarvis-auto-improver` | After completing features, when code quality can be improved |
| `jarvis-code-reviewer` | After writing/modifying code in this project |
| `jarvis-gpu-manager` | GPU allocation, VRAM optimization, thermal management, model loading |
| `jarvis-devops` | Infrastructure changes, CI/CD, deployment, cluster configuration |
| `jarvis-browser` | Web browsing tasks, BrowserOS orchestration, automated web workflows |
| `jarvis-voice-controller` | Voice command processing, EasySpeak management |
| `feature-dev:code-explorer` | When deep codebase analysis is needed before implementation |
| `feature-dev:code-architect` | When designing new features or refactoring architecture |
| `feature-dev:code-reviewer` | After writing code — review for bugs, security, quality |
| `code-simplifier:code-simplifier` | After completing code — simplify for clarity |
| `pr-review-toolkit:silent-failure-hunter` | After writing error handling, catch blocks, fallback logic |
| `pr-review-toolkit:code-reviewer` | Before commits — check style and guidelines |
| `pr-review-toolkit:pr-test-analyzer` | Before PRs — verify test coverage |
| `pr-review-toolkit:type-design-analyzer` | When introducing new types |
| `pr-review-toolkit:comment-analyzer` | After writing documentation/comments |
| `plugin-dev:plugin-validator` | After creating/modifying plugin components |
| `plugin-dev:skill-reviewer` | After creating/modifying skills |
| `plugin-dev:agent-creator` | When user asks to create a new agent |

### Skill Auto-Invoke Matrix

Invoke these skills automatically when their context applies:

| Skill | Auto-Invoke When |
|-------|------------------|
| `superpowers:brainstorming` | Before ANY creative work — features, components, modifications |
| `superpowers:writing-plans` | Before multi-step implementation tasks |
| `superpowers:executing-plans` | When executing a written plan |
| `superpowers:test-driven-development` | Before implementing features or bugfixes |
| `superpowers:systematic-debugging` | On ANY bug, test failure, or unexpected behavior |
| `superpowers:verification-before-completion` | Before claiming work is done |
| `superpowers:requesting-code-review` | After completing features |
| `superpowers:receiving-code-review` | When processing review feedback |
| `superpowers:dispatching-parallel-agents` | When 2+ independent tasks exist |
| `superpowers:using-git-worktrees` | For feature work needing isolation |
| `superpowers:finishing-a-development-branch` | When implementation is complete |
| `feature-dev:feature-dev` | For guided feature development |
| `frontend-design:frontend-design` | When building web UI components |
| `claude-api` | When code imports anthropic/claude SDKs |
| `commit-commands:commit` | When user asks to commit |
| `commit-commands:commit-push-pr` | When user asks to commit, push, and PR |
| `code-review:code-review` | When user asks for code review |
| `pr-review-toolkit:review-pr` | For comprehensive PR review |
| `claude-md-management:revise-claude-md` | To update this file with learnings |
| `claude-md-management:claude-md-improver` | To audit CLAUDE.md quality |
| `plugin-dev:create-plugin` | When creating new plugins |
| `plugin-dev:agent-development` | When creating/modifying agents |
| `plugin-dev:skill-development` | When creating/modifying skills |
| `plugin-dev:command-development` | When creating/modifying commands |
| `plugin-dev:hook-development` | When creating/modifying hooks |
| `plugin-dev:mcp-integration` | When integrating MCP servers |
| `skill-creator:skill-creator` | When creating/optimizing skills |
| `playground:playground` | When building interactive HTML tools |
| `cluster-management` | For JARVIS cluster operations |
| `failover-recovery` | When nodes fail or need recovery |
| `performance-tuning` | For GPU/cluster optimization |
| `security-audit` | For security assessments |
| `smart-routing` | For task routing across cluster |
| `weighted-orchestration` | For multi-agent consensus |
| `trading-pipeline` | For trading operations |
| `browser-workflow` | For BrowserOS automation |
| `mao-workflow` | For Multi-Agent Orchestration workflows |
| `continuous-improvement` | For self-improvement cycles |
| `autotest-analysis` | For test analysis |

### Slash Commands Quick Reference

| Command | Purpose |
|---------|---------|
| `/cluster-check` | Health check all cluster nodes |
| `/gpu-status` | GPU temperature, VRAM, utilization |
| `/diagnostic` | Full system diagnostic |
| `/heal-cluster` | Auto-heal offline nodes |
| `/consensus` | Multi-agent consensus query |
| `/trading-scan` | Market scan via trading pipeline |
| `/trade` | Execute trading operations |
| `/browse` | BrowserOS navigation |
| `/models` | List available AI models |
| `/content` | Content generation |
| `/ask-ai` | Query local AI models |
| `/os` | System operations |
| `/valise-list` | List browser valises |
| `/valise-run` | Execute a browser valise |
| `/valise-new` | Create new browser valise |
| `/commit` | Git commit |
| `/commit-push-pr` | Commit + push + PR |
| `/code-review` | Code review |
| `/review-pr` | Comprehensive PR review |
| `/feature-dev` | Guided feature development |
| `/create-plugin` | Create new plugin |
| `/ralph-loop` | Start Ralph self-referential loop |
| `/revise-claude-md` | Update CLAUDE.md |

---

## Infrastructure

### Cluster Nodes

| Node | Role | Endpoint |
|------|------|----------|
| M1 LMStudio | gemma-3-4b (0.4s rapide) + qwen3.5-9b + deepseek-r1 | `http://127.0.0.1:1234` |
| M3 (remote) | deepseek-r1-qwen3-8b — **champion fiabilite 100%** | `http://192.168.1.113:1234` |
| OL1 | Ollama: qwen2.5:1.5b, deepseek-r1:7b | `http://127.0.0.1:11434` |
| M2 (remote) | deepseek-coder — instable | `http://192.168.1.26:1234` |

### MCP Servers

| Server | Type | Purpose |
|--------|------|---------|
| `jarvis` | Custom | Main cluster orchestration |
| `jarvis-m1` | Custom | M1 node control |
| `jarvis-ol1` | Custom | OL1 node control |
| `browseros` | HTTP | Browser automation (56 tools + 47 integrations) |
| `chrome-devtools` | stdio | Chrome DevTools inspection |
| `comet` | MCP | Perplexity AI browser |
| `github` | MCP | GitHub API |
| `playwright` | MCP | Browser testing |
| `Google Calendar` | MCP | Calendar management |
| `Canva` | MCP | Design automation |
| `Notion` | MCP | Knowledge base |

### Browser Automation (BrowserOS + CDP)

BrowserOS is running on M1 with Chrome DevTools Protocol on port 9222. All agents can control the browser:

**Connection:** `ws://127.0.0.1:9222/devtools/page/{TAB_ID}`
**Tab list:** `curl -s http://127.0.0.1:9222/json`
**Python module:** `from scripts.browser_agent import BrowserAgent, BrowserAgentSync`

**Quick usage in agents:**
```python
from scripts.browser_agent import BrowserAgentSync
browser = BrowserAgentSync()
browser.navigate("https://example.com")
browser.fill("#search", "query")
browser.click("#submit")
text = browser.get_text(".result")
browser.screenshot("/tmp/result.png")
```

**CDP capabilities:**
- Navigate, click, fill, type, scroll, screenshot
- Tab management: list, create, switch, close, group
- JavaScript evaluation on any page
- Form submission, copy/paste
- Works with authenticated sessions (BrowserOS keeps cookies)

**MCP Integration:**
- Chrome DevTools MCP: `npx chrome-devtools-mcp@latest --cdp-endpoint http://127.0.0.1:9222`
- Playwright MCP: `npx @playwright/mcp@latest --cdp-endpoint http://127.0.0.1:9222`

**Rules:**
- Always check `curl -s http://127.0.0.1:9222/json | python3 -c "import sys,json; [print(f'{t[\"id\"]}: {t[\"title\"][:50]}') for t in json.load(sys.stdin) if t.get(\"type\")==\"page\"]"` before interacting
- Use existing authenticated tabs when possible (Codeur, LinkedIn, etc.)
- Take screenshot after important actions for verification
- Handle tab switching carefully — CDP commands go to the selected tab

### GPU Safety Rules

- **Thermal guard**: Block GPU operations if any GPU > 85C
- **VRAM management**: Monitor allocation before loading models
- **Failover cascade**: M3→OL1→M1→M2→GEMINI→CLAUDE (benchmark 2026-03-27)
- Never fallback CPU-bound tasks to asyncio loop

---

## Development Standards

### Code Style
- Python: snake_case, type hints, async/await for I/O
- TypeScript: camelCase, strict types
- All code: minimal, no premature abstractions
- Comments only where logic isn't self-evident

### Git Workflow
- Branch naming: `feature/`, `fix/`, `refactor/`
- Commit messages in English, concise
- Always verify before claiming done
- Never force-push to main

### Testing
- TDD when implementing features
- Integration tests hit real services, no mocks for critical paths
- Verify GPU operations with thermal check first

---

## Parallel Agent Strategy

When facing complex tasks, dispatch agents in parallel:
- Use `superpowers:dispatching-parallel-agents` for 2+ independent tasks
- Launch `task-decomposer-prime` for multi-domain requests
- Use `feature-dev:code-explorer` + `feature-dev:code-architect` in parallel for new features
- Post-implementation: run `code-reviewer` + `code-simplifier` + `silent-failure-hunter` in parallel

---

## Plugin Ecosystem

### Local Plugins (custom)
- **jarvis-turbo** v3.0.0 — Core cluster management (7 agents, 12 commands, 11 skills, 2 hooks)
- **jarvis-valises** v1.0.0 — Browser automation packs (3 commands, 1 skill, 10 valises)

### Marketplace Plugins (28 enabled)
All from `claude-plugins-official` — development tools, AI/ML, output styles, automation, integrations.

### Auto-Discovery
All `.md` files in `agents/`, `commands/`, `skills/`, `hooks/` directories are auto-discovered by Claude Code.
