---
name: Auto-Activation Preference
description: User wants ALL agents and skills triggered automatically without waiting for explicit requests
type: feedback
---

User explicitly requested automatic startup and usage of all agents and skills.

**Why:** Turbo31150 operates at expert level and trusts Claude to proactively use the full ecosystem. Manual invocation slows down the workflow.

**How to apply:**
- Never wait for user to ask "use agent X" — if conditions match, trigger it
- Dispatch multiple agents in parallel when independent tasks exist
- Run post-implementation review agents (code-reviewer, simplifier, silent-failure-hunter) automatically after writing code
- Use superpowers skills (brainstorming, TDD, debugging, verification) proactively at their trigger points
- Launch cluster health checks at session start
- Invoke feature-dev skills when starting any development work
