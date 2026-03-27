---
name: Browser Automation System
description: CDP-based browser automation for JARVIS agents — BrowserOS port 9108, auto-learning navigation cache
type: project
---

JARVIS has a full browser automation system via Chrome DevTools Protocol.

**Active BrowserOS:** Port 9108 (visible, with authenticated sessions for LinkedIn, ChatGPT, Claude, Gemini, Perplexity, AI Studio)
**Backup CDP:** Port 9222 (headless chrome for background tasks)

**Key files:**
- `scripts/cdp.py` — 33 functions, fast CDP wrapper with auto-learning cache
- `scripts/browser_agent.py` — 469-line async/sync module for agents
- `data/browser-nav-cache.json` — auto-learned selectors (7 sites, 8 selector sets)
- `data/browser-speed-routes.json` — pre-configured fast routes (18 routes for 5 sites)
- `scripts/browseros-persistent.sh` — launcher with cookie persistence
- `scripts/post-login-actions.py` — automated post-login workflow

**Why:** Enables agents to interact with authenticated web platforms (Codeur, LinkedIn) without manual copy-paste. Each navigation enriches the cache for faster future interactions.

**How to apply:**
- Always use port 9108 for user-visible actions
- Import `cdp.py` for quick scripts, `browser_agent.py` for agent integration
- Check `browser-nav-cache.json` before scanning — selectors may already be cached
- LinkedIn comment posting works: navigate to activity URL → fill contenteditable → click "Commenter"
- Codeur profile edit: `/users/733953/profile/edit` → textareas `#user_profile_biography` + `#user_profile_presentation`
