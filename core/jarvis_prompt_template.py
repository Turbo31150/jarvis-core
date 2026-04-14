#!/usr/bin/env python3
"""
jarvis_prompt_template — Jinja2-style prompt template engine
Manages reusable prompt templates with variables, versioning, A/B testing
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.prompt_template")

TEMPLATES_DIR = Path("/home/turbo/IA/Core/jarvis/data/prompt_templates")
REDIS_PREFIX = "jarvis:template:"
INDEX_KEY = "jarvis:templates:index"


@dataclass
class PromptTemplate:
    name: str
    template: str
    description: str = ""
    version: int = 1
    variables: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    use_count: int = 0
    avg_score: float = 0.0

    def __post_init__(self):
        # Auto-detect variables from {var} placeholders
        if not self.variables:
            self.variables = re.findall(r"\{(\w+)\}", self.template)

    def render(self, **kwargs) -> str:
        """Render template with provided variables."""
        missing = [v for v in self.variables if v not in kwargs]
        if missing:
            raise ValueError(f"Missing variables: {missing}")
        result = self.template
        for k, v in kwargs.items():
            result = result.replace(f"{{{k}}}", str(v))
        return result

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "template": self.template,
            "description": self.description,
            "version": self.version,
            "variables": self.variables,
            "tags": self.tags,
            "created_at": self.created_at,
            "use_count": self.use_count,
            "avg_score": self.avg_score,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PromptTemplate":
        return cls(
            name=d["name"],
            template=d["template"],
            description=d.get("description", ""),
            version=d.get("version", 1),
            variables=d.get("variables", []),
            tags=d.get("tags", []),
            created_at=d.get("created_at", time.time()),
            use_count=d.get("use_count", 0),
            avg_score=d.get("avg_score", 0.0),
        )


# ── Built-in templates ────────────────────────────────────────────────────────

BUILTIN_TEMPLATES = [
    PromptTemplate(
        name="summarize",
        template="Résume ce texte en {max_words} mots maximum:\n\n{text}",
        description="Summarize text",
        tags=["text", "summary"],
    ),
    PromptTemplate(
        name="classify",
        template="Classe ce texte dans une de ces catégories: {categories}\n\nTexte: {text}\n\nRéponds avec juste le nom de la catégorie.",
        description="Classify text into categories",
        tags=["classification"],
    ),
    PromptTemplate(
        name="extract_json",
        template="Extrait les informations suivantes de ce texte et retourne un JSON valide avec les clés: {fields}\n\nTexte: {text}\n\nJSON:",
        description="Extract structured data as JSON",
        tags=["extraction", "json"],
    ),
    PromptTemplate(
        name="code_review",
        template="Revois ce code {language} et identifie les problèmes de qualité, bugs, sécurité:\n\n```{language}\n{code}\n```\n\nRapport concis:",
        description="Code review",
        tags=["code", "review"],
    ),
    PromptTemplate(
        name="translate",
        template="Traduis ce texte de {source_lang} vers {target_lang}. Garde le ton et le style.\n\n{text}",
        description="Translate text",
        tags=["translation"],
    ),
    PromptTemplate(
        name="qa_context",
        template="Contexte:\n{context}\n\nQuestion: {question}\n\nRéponds uniquement à partir du contexte fourni. Si la réponse n'est pas dans le contexte, dis-le.",
        description="Q&A with context (RAG)",
        tags=["rag", "qa"],
    ),
    PromptTemplate(
        name="agent_task",
        template="Tu es {agent_name}. Ton rôle: {agent_role}\n\nTâche: {task}\n\nContraintes: {constraints}\n\nRéponds de manière structurée.",
        description="Agent task execution",
        tags=["agent"],
    ),
    PromptTemplate(
        name="debug_error",
        template="Analyse cette erreur {language} et propose un fix:\n\nErreur:\n{error}\n\nCode concerné:\n```{language}\n{code}\n```\n\nCause probable et solution:",
        description="Debug error and suggest fix",
        tags=["debug", "code"],
    ),
]


class PromptTemplateEngine:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._templates: dict[str, PromptTemplate] = {}
        self._init_builtins()
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    def _init_builtins(self):
        for t in BUILTIN_TEMPLATES:
            self._templates[t.name] = t

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            await self._load_from_redis()
        except Exception:
            self.redis = None

    async def _load_from_redis(self):
        if not self.redis:
            return
        names = await self.redis.smembers(INDEX_KEY)
        for name in names:
            raw = await self.redis.get(f"{REDIS_PREFIX}{name}")
            if raw:
                t = PromptTemplate.from_dict(json.loads(raw))
                self._templates[t.name] = t

    async def save(self, template: PromptTemplate):
        self._templates[template.name] = template
        # Save to file
        path = TEMPLATES_DIR / f"{template.name}.json"
        path.write_text(json.dumps(template.to_dict(), indent=2))
        # Save to Redis
        if self.redis:
            await self.redis.set(
                f"{REDIS_PREFIX}{template.name}",
                json.dumps(template.to_dict()),
            )
            await self.redis.sadd(INDEX_KEY, template.name)

    def get(self, name: str) -> PromptTemplate | None:
        return self._templates.get(name)

    def render(self, name: str, **kwargs) -> str:
        t = self.get(name)
        if not t:
            raise KeyError(f"Template '{name}' not found")
        t.use_count += 1
        return t.render(**kwargs)

    def search(self, query: str) -> list[PromptTemplate]:
        q = query.lower()
        return [
            t
            for t in self._templates.values()
            if q in t.name.lower()
            or q in t.description.lower()
            or any(q in tag for tag in t.tags)
        ]

    def list_all(self) -> list[dict]:
        return sorted(
            [
                {
                    "name": t.name,
                    "description": t.description,
                    "variables": t.variables,
                    "tags": t.tags,
                    "use_count": t.use_count,
                }
                for t in self._templates.values()
            ],
            key=lambda x: -x["use_count"],
        )

    async def record_score(self, name: str, score: float):
        t = self.get(name)
        if not t:
            return
        # Exponential moving average
        alpha = 0.2
        t.avg_score = alpha * score + (1 - alpha) * t.avg_score
        await self.save(t)


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    engine = PromptTemplateEngine()
    await engine.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        templates = engine.list_all()
        print(f"{'Name':<20} {'Variables':<30} {'Tags':<20} {'Uses':>5}")
        print("-" * 80)
        for t in templates:
            print(
                f"{t['name']:<20} {', '.join(t['variables']):<30} "
                f"{', '.join(t['tags']):<20} {t['use_count']:>5}"
            )

    elif cmd == "get" and len(sys.argv) > 2:
        t = engine.get(sys.argv[2])
        if t:
            print(json.dumps(t.to_dict(), indent=2))
        else:
            print(f"Template '{sys.argv[2]}' not found")

    elif cmd == "render" and len(sys.argv) > 2:
        name = sys.argv[2]
        # Parse key=value args
        kwargs = {}
        for arg in sys.argv[3:]:
            if "=" in arg:
                k, v = arg.split("=", 1)
                kwargs[k] = v
        try:
            result = engine.render(name, **kwargs)
            print(result)
        except (KeyError, ValueError) as e:
            print(f"Error: {e}")

    elif cmd == "search" and len(sys.argv) > 2:
        results = engine.search(sys.argv[2])
        for t in results:
            print(f"  {t.name}: {t.description} {t.tags}")


if __name__ == "__main__":
    asyncio.run(main())
