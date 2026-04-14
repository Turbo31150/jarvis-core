#!/usr/bin/env python3
"""
jarvis_prompt_builder — Template-based prompt construction with variable substitution
Build structured prompts with slots, few-shot examples, and chain-of-thought scaffolding
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.prompt_builder")

REDIS_PREFIX = "jarvis:prompts:"
TEMPLATES_FILE = Path("/home/turbo/IA/Core/jarvis/data/prompt_templates.json")


@dataclass
class PromptTemplate:
    name: str
    system: str = ""
    user_template: str = ""
    assistant_prefix: str = ""  # prime the assistant's first token
    few_shot_examples: list[dict] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    version: int = 1
    created_at: float = field(default_factory=time.time)

    def slots(self) -> list[str]:
        """Return all {{slot_name}} placeholders in all template fields."""
        pattern = r"\{\{(\w+)\}\}"
        found = set()
        for text in [self.system, self.user_template, self.assistant_prefix]:
            found.update(re.findall(pattern, text))
        return sorted(found)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "system": self.system,
            "user_template": self.user_template,
            "assistant_prefix": self.assistant_prefix,
            "few_shot_examples": self.few_shot_examples,
            "description": self.description,
            "tags": self.tags,
            "version": self.version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PromptTemplate":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class BuiltPrompt:
    template_name: str
    messages: list[dict]
    variables_used: dict
    missing_variables: list[str]
    token_estimate: int
    duration_ms: float

    def to_dict(self) -> dict:
        return {
            "template_name": self.template_name,
            "message_count": len(self.messages),
            "variables_used": list(self.variables_used.keys()),
            "missing_variables": self.missing_variables,
            "token_estimate": self.token_estimate,
            "duration_ms": round(self.duration_ms, 1),
        }


def _substitute(template: str, variables: dict[str, Any]) -> tuple[str, list[str]]:
    """Replace {{slot}} with value. Returns (result, list_of_missing_slots)."""
    missing = []
    result = template

    def replacer(match: re.Match) -> str:
        key = match.group(1)
        if key in variables:
            return str(variables[key])
        missing.append(key)
        return match.group(0)  # keep original if missing

    result = re.sub(r"\{\{(\w+)\}\}", replacer, result)
    return result, missing


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class PromptBuilder:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._templates: dict[str, PromptTemplate] = {}
        self._stats: dict[str, int] = {
            "built": 0,
            "template_hits": 0,
            "missing_vars": 0,
        }
        self._load()

    def _load(self):
        if TEMPLATES_FILE.exists():
            try:
                data = json.loads(TEMPLATES_FILE.read_text())
                for td in data.get("templates", []):
                    t = PromptTemplate.from_dict(td)
                    self._templates[t.name] = t
                log.debug(f"Loaded {len(self._templates)} prompt templates")
            except Exception as e:
                log.warning(f"Template load error: {e}")

    def _save(self):
        TEMPLATES_FILE.parent.mkdir(parents=True, exist_ok=True)
        TEMPLATES_FILE.write_text(
            json.dumps(
                {"templates": [t.to_dict() for t in self._templates.values()]}, indent=2
            )
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register(self, template: PromptTemplate):
        self._templates[template.name] = template
        self._save()

    def get(self, name: str) -> PromptTemplate | None:
        return self._templates.get(name)

    def build(
        self,
        template_name: str,
        variables: dict[str, Any] | None = None,
        extra_context: str = "",
        cot: bool = False,
    ) -> BuiltPrompt:
        t0 = time.time()
        self._stats["built"] += 1
        vars_ = variables or {}
        template = self._templates.get(template_name)

        if not template:
            # Build ad-hoc from raw template_name as user text
            messages = [{"role": "user", "content": template_name}]
            return BuiltPrompt(
                template_name="(raw)",
                messages=messages,
                variables_used={},
                missing_variables=[],
                token_estimate=_estimate_tokens(template_name),
                duration_ms=(time.time() - t0) * 1000,
            )

        self._stats["template_hits"] += 1
        all_missing: list[str] = []
        messages: list[dict] = []

        # System message
        system_text = template.system
        if extra_context:
            system_text = (
                extra_context + "\n\n" + system_text if system_text else extra_context
            )
        if cot:
            system_text += "\n\nThink step by step before answering."
        if system_text:
            substituted, missing = _substitute(system_text, vars_)
            all_missing.extend(missing)
            messages.append({"role": "system", "content": substituted})

        # Few-shot examples
        for example in template.few_shot_examples:
            if "user" in example:
                us, _ = _substitute(example["user"], vars_)
                messages.append({"role": "user", "content": us})
            if "assistant" in example:
                ai, _ = _substitute(example["assistant"], vars_)
                messages.append({"role": "assistant", "content": ai})

        # User message
        if template.user_template:
            substituted, missing = _substitute(template.user_template, vars_)
            all_missing.extend(missing)
            messages.append({"role": "user", "content": substituted})

        # Assistant prefix (prime)
        if template.assistant_prefix:
            substituted, missing = _substitute(template.assistant_prefix, vars_)
            all_missing.extend(missing)
            messages.append({"role": "assistant", "content": substituted})

        if all_missing:
            self._stats["missing_vars"] += len(all_missing)

        token_est = sum(_estimate_tokens(m["content"]) for m in messages)

        return BuiltPrompt(
            template_name=template_name,
            messages=messages,
            variables_used=vars_,
            missing_variables=list(set(all_missing)),
            token_estimate=token_est,
            duration_ms=(time.time() - t0) * 1000,
        )

    def build_chat(
        self,
        system: str,
        user: str,
        history: list[dict] | None = None,
        assistant_prefix: str = "",
    ) -> list[dict]:
        """Quick builder without template."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        for msg in history or []:
            messages.append(msg)
        messages.append({"role": "user", "content": user})
        if assistant_prefix:
            messages.append({"role": "assistant", "content": assistant_prefix})
        return messages

    def list_templates(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "slots": t.slots(),
                "description": t.description,
                "tags": t.tags,
            }
            for t in self._templates.values()
        ]

    def stats(self) -> dict:
        return {**self._stats, "templates": len(self._templates)}


def build_jarvis_builder() -> PromptBuilder:
    builder = PromptBuilder()
    builder.register(
        PromptTemplate(
            name="code_review",
            system="You are a senior Python engineer. Review the code for bugs, security issues, and style violations.",
            user_template="Review this code:\n```python\n{{code}}\n```\nFocus on: {{focus}}",
            description="Code review with focus area",
            tags=["code", "review"],
        )
    )
    builder.register(
        PromptTemplate(
            name="summarize",
            system="Summarize the following content concisely. Output {{format}}.",
            user_template="{{content}}",
            assistant_prefix="Summary:",
            description="Content summarizer",
            tags=["summarize"],
        )
    )
    builder.register(
        PromptTemplate(
            name="cluster_analysis",
            system="You are JARVIS, analyzing GPU cluster health. Be technical and concise.",
            user_template="Cluster metrics:\n{{metrics}}\n\nIdentify issues and recommendations.",
            few_shot_examples=[
                {
                    "user": "GPU temp: 85C",
                    "assistant": "⚠️ GPU thermal throttling risk. Reduce load or increase cooling.",
                },
            ],
            description="Cluster health analysis",
            tags=["cluster", "ops"],
        )
    )
    return builder


async def main():
    import sys

    builder = build_jarvis_builder()
    await builder.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Code review
        result = builder.build(
            "code_review",
            {
                "code": "def divide(a, b):\n    return a / b",
                "focus": "error handling",
            },
        )
        print(f"Template: {result.template_name}")
        print(f"Messages: {len(result.messages)}")
        for msg in result.messages:
            print(f"  [{msg['role']}] {msg['content'][:80]}")
        print(f"Tokens: ~{result.token_estimate}")
        if result.missing_variables:
            print(f"Missing: {result.missing_variables}")

        # Missing vars test
        result2 = builder.build("summarize", {"format": "bullet points"})
        print(f"\nMissing vars: {result2.missing_variables}")

        print(f"\nTemplates: {json.dumps(builder.list_templates(), indent=2)[:400]}")
        print(f"\nStats: {json.dumps(builder.stats(), indent=2)}")

    elif cmd == "list":
        for t in builder.list_templates():
            print(f"  {t['name']:<25} slots={t['slots']} tags={t['tags']}")

    elif cmd == "stats":
        print(json.dumps(builder.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
