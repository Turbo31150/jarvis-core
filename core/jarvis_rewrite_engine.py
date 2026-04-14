#!/usr/bin/env python3
"""
jarvis_rewrite_engine — Text rewriting and transformation pipeline
Chain-of-rewriters, rule-based + LLM-assisted transforms, diff tracking
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger("jarvis.rewrite_engine")


class RewriteOp(str, Enum):
    TRIM = "trim"  # remove whitespace/redundancy
    REPHRASE = "rephrase"  # change wording, keep meaning
    EXPAND = "expand"  # add detail
    COMPRESS = "compress"  # reduce length
    FORMALIZE = "formalize"  # formal register
    SIMPLIFY = "simplify"  # plain language
    TRANSLATE = "translate"  # language conversion
    REDACT = "redact"  # remove sensitive info
    ANONYMIZE = "anonymize"  # replace PII
    EXTRACT = "extract"  # pull specific content
    INJECT = "inject"  # add prefix/suffix/template
    REGEX_REPLACE = "regex"  # pattern-based replace
    CUSTOM = "custom"  # user-provided fn


class RewriteStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"  # condition not met
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass
class RewriteRule:
    name: str
    op: RewriteOp
    # Op-specific config
    pattern: str = ""  # for REGEX_REPLACE / REDACT
    replacement: str = ""  # for REGEX_REPLACE
    target_lang: str = ""  # for TRANSLATE
    max_tokens: int = 0  # for COMPRESS
    prefix: str = ""  # for INJECT
    suffix: str = ""  # for INJECT
    template: str = ""  # for INJECT — use {text} placeholder
    min_length: int = 0  # run only if text len >= min_length
    max_length: int = 0  # run only if text len <= max_length (0 = no limit)
    enabled: bool = True
    priority: int = 0  # lower = runs first


@dataclass
class RewriteStep:
    rule: RewriteRule
    input_text: str
    output_text: str
    status: RewriteStatus
    duration_ms: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "rule": self.rule.name,
            "op": self.rule.op.value,
            "status": self.status.value,
            "input_len": len(self.input_text),
            "output_len": len(self.output_text),
            "delta": len(self.output_text) - len(self.input_text),
            "duration_ms": round(self.duration_ms, 2),
            "note": self.note,
        }


@dataclass
class RewriteResult:
    original: str
    final: str
    steps: list[RewriteStep] = field(default_factory=list)
    success: bool = True
    total_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.original != self.final

    @property
    def compression_ratio(self) -> float:
        if not self.original:
            return 1.0
        return len(self.final) / len(self.original)

    def diff_summary(self) -> str:
        delta = len(self.final) - len(self.original)
        pct = abs(delta) / max(len(self.original), 1) * 100
        sign = "+" if delta >= 0 else "-"
        return f"{sign}{abs(delta)} chars ({pct:.1f}%)"

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "changed": self.changed,
            "original_len": len(self.original),
            "final_len": len(self.final),
            "diff": self.diff_summary(),
            "steps": [s.to_dict() for s in self.steps],
            "total_ms": round(self.total_ms, 2),
        }


# --- Built-in rule-based rewriters ---

_PII_PATTERNS = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[PHONE]"),
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[CARD]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\bhttps?://\S+"), "[URL]"),
    (
        re.compile(r"\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+", re.I),
        "[REDACTED_CRED]",
    ),
]


def _apply_trim(text: str, _rule: "RewriteRule", _ctx: dict) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def _apply_compress(text: str, rule: "RewriteRule", _ctx: dict) -> str:
    fillers = [
        r"\bIn conclusion,?\s*",
        r"\bTo summarize,?\s*",
        r"\bIt is worth noting that\s*",
        r"\bAs we can see,?\s*",
        r"\bBasically,?\s*",
        r"\bEssentially,?\s*",
        r"\bOf course,?\s*",
        r"\bNeedless to say,?\s*",
        r"\bIt is important to note that\s*",
        r"\bClearly,?\s*",
    ]
    for f in fillers:
        text = re.sub(f, "", text, flags=re.I)
    if rule.max_tokens > 0:
        words = text.split()
        if len(words) > rule.max_tokens:
            text = " ".join(words[: rule.max_tokens]) + "\u2026"
    return text.strip()


def _apply_simplify(text: str, _rule: "RewriteRule", _ctx: dict) -> str:
    replacements = {
        r"\butilize\b": "use",
        r"\bcommence\b": "start",
        r"\bfacilitate\b": "help",
        r"\bascertain\b": "find out",
        r"\bsubsequently\b": "then",
        r"\bnevertheless\b": "but",
        r"\bnotwithstanding\b": "despite",
        r"\bmethodology\b": "method",
        r"\bparadigm\b": "model",
        r"\bsynergize\b": "work together",
        r"\bleverage\b": "use",
    }
    for pat, repl in replacements.items():
        text = re.sub(pat, repl, text, flags=re.I)
    return text


def _apply_formalize(text: str, _rule: "RewriteRule", _ctx: dict) -> str:
    replacements = {
        r"\bdon't\b": "do not",
        r"\bcan't\b": "cannot",
        r"\bwon't\b": "will not",
        r"\bisn't\b": "is not",
        r"\baren't\b": "are not",
        r"\bwasn't\b": "was not",
        r"\bweren't\b": "were not",
        r"\bhadn't\b": "had not",
        r"\bhasn't\b": "has not",
        r"\bhaven't\b": "have not",
        r"\bI'm\b": "I am",
        r"\bI've\b": "I have",
        r"\bI'll\b": "I will",
        r"\bI'd\b": "I would",
        r"\byou're\b": "you are",
        r"\bthey're\b": "they are",
        r"\bwe're\b": "we are",
        r"\bit's\b": "it is",
        r"\bthat's\b": "that is",
        r"\bgonna\b": "going to",
        r"\bwanna\b": "want to",
        r"\bgotta\b": "have to",
        r"\bkinda\b": "kind of",
        r"\bsorta\b": "sort of",
        r"\byeah\b": "yes",
        r"\bnope\b": "no",
    }
    for pat, repl in replacements.items():
        text = re.sub(pat, repl, text, flags=re.I)
    return text


def _apply_anonymize(text: str, _rule: "RewriteRule", _ctx: dict) -> str:
    for pattern, placeholder in _PII_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def _apply_redact(text: str, rule: "RewriteRule", _ctx: dict) -> str:
    if rule.pattern:
        pat = re.compile(rule.pattern, re.I)
        repl = rule.replacement or "[REDACTED]"
        text = pat.sub(repl, text)
    else:
        for pattern, placeholder in _PII_PATTERNS[-1:]:
            text = pattern.sub(placeholder, text)
    return text


def _apply_regex_replace(text: str, rule: "RewriteRule", _ctx: dict) -> str:
    if not rule.pattern:
        return text
    pat = re.compile(rule.pattern, re.DOTALL)
    return pat.sub(rule.replacement, text)


def _apply_inject(text: str, rule: "RewriteRule", _ctx: dict) -> str:
    if rule.template:
        return rule.template.replace("{text}", text)
    result = text
    if rule.prefix:
        result = rule.prefix + result
    if rule.suffix:
        result = result + rule.suffix
    return result


_BUILTIN_HANDLERS: dict[RewriteOp, Callable] = {
    RewriteOp.TRIM: _apply_trim,
    RewriteOp.COMPRESS: _apply_compress,
    RewriteOp.SIMPLIFY: _apply_simplify,
    RewriteOp.FORMALIZE: _apply_formalize,
    RewriteOp.ANONYMIZE: _apply_anonymize,
    RewriteOp.REDACT: _apply_redact,
    RewriteOp.REGEX_REPLACE: _apply_regex_replace,
    RewriteOp.INJECT: _apply_inject,
}


def _check_length_condition(text: str, rule: RewriteRule) -> bool:
    """Return False (skip) if text length violates rule's min/max constraints."""
    n = len(text)
    if rule.min_length > 0 and n < rule.min_length:
        return False
    if rule.max_length > 0 and n > rule.max_length:
        return False
    return True


class RewriteEngine:
    def __init__(self):
        self._rules: list[RewriteRule] = []
        self._custom_handlers: dict[str, Callable[[str, RewriteRule, dict], str]] = {}
        self._stats: dict[str, int] = {
            "rewrites": 0,
            "steps_run": 0,
            "steps_skipped": 0,
            "steps_failed": 0,
        }

    def add_rule(self, rule: RewriteRule):
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)

    def register_handler(
        self, name: str, fn: Callable[[str, "RewriteRule", dict], str]
    ):
        self._custom_handlers[name] = fn

    def remove_rule(self, name: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.name != name]
        return len(self._rules) < before

    def rewrite(
        self,
        text: str,
        context: dict | None = None,
        rule_names: list[str] | None = None,
    ) -> RewriteResult:
        t0 = time.time()
        self._stats["rewrites"] += 1
        ctx = context or {}
        current = text

        rules = (
            self._rules
            if rule_names is None
            else [r for r in self._rules if r.name in rule_names]
        )

        steps: list[RewriteStep] = []

        for rule in rules:
            if not rule.enabled:
                continue

            step_t0 = time.time()
            input_text = current

            if not _check_length_condition(current, rule):
                steps.append(
                    RewriteStep(
                        rule=rule,
                        input_text=input_text,
                        output_text=current,
                        status=RewriteStatus.SKIPPED,
                        note="length condition",
                    )
                )
                self._stats["steps_skipped"] += 1
                continue

            try:
                if rule.op == RewriteOp.CUSTOM:
                    handler = self._custom_handlers.get(rule.name)
                    if not handler:
                        raise ValueError(f"no custom handler for {rule.name!r}")
                    current = handler(current, rule, ctx)
                else:
                    handler_fn = _BUILTIN_HANDLERS.get(rule.op)
                    if not handler_fn:
                        raise ValueError(f"unsupported op: {rule.op}")
                    current = handler_fn(current, rule, ctx)

                dur = (time.time() - step_t0) * 1000
                steps.append(
                    RewriteStep(
                        rule=rule,
                        input_text=input_text,
                        output_text=current,
                        status=RewriteStatus.SUCCESS,
                        duration_ms=dur,
                    )
                )
                self._stats["steps_run"] += 1

            except Exception as e:
                dur = (time.time() - step_t0) * 1000
                steps.append(
                    RewriteStep(
                        rule=rule,
                        input_text=input_text,
                        output_text=current,
                        status=RewriteStatus.FAILED,
                        duration_ms=dur,
                        note=str(e),
                    )
                )
                self._stats["steps_failed"] += 1
                log.warning(f"Rule {rule.name!r} failed: {e}")

        return RewriteResult(
            original=text,
            final=current,
            steps=steps,
            success=True,
            total_ms=(time.time() - t0) * 1000,
        )

    def rewrite_many(self, texts: list[str], **kwargs) -> list[RewriteResult]:
        return [self.rewrite(t, **kwargs) for t in texts]

    def stats(self) -> dict:
        return {**self._stats, "rules": len(self._rules)}


def build_jarvis_rewrite_engine() -> RewriteEngine:
    eng = RewriteEngine()

    eng.add_rule(
        RewriteRule(
            name="trim_whitespace",
            op=RewriteOp.TRIM,
            priority=0,
        )
    )
    eng.add_rule(
        RewriteRule(
            name="anonymize_pii",
            op=RewriteOp.ANONYMIZE,
            priority=10,
        )
    )
    eng.add_rule(
        RewriteRule(
            name="remove_think_tags",
            op=RewriteOp.REGEX_REPLACE,
            pattern=r"<think>.*?</think>",
            replacement="",
            priority=5,
        )
    )
    eng.add_rule(
        RewriteRule(
            name="compress_long",
            op=RewriteOp.COMPRESS,
            max_tokens=500,
            min_length=2000,
            priority=20,
        )
    )

    return eng


def main():
    import sys

    eng = build_jarvis_rewrite_engine()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Rewrite engine demo...\n")

        samples = [
            "Hello!  \n\nThis is a test.\n\n\n\nWith multiple blank lines.",
            "My email is alice@example.com and phone is 555-123-4567. Don't forget my API_KEY=secret123.",
            "<think>internal reasoning</think>The answer is 42.",
            "Basically, it is important to note that we should utilize this methodology.",
        ]

        for sample in samples:
            result = eng.rewrite(sample)
            print(f"  IN:  {repr(sample[:70])}")
            print(f"  OUT: {repr(result.final[:70])}")
            print(f"  {result.diff_summary()} | steps={len(result.steps)}")
            for s in result.steps:
                icon = (
                    "✅"
                    if s.status == RewriteStatus.SUCCESS
                    else "⏭"
                    if s.status == RewriteStatus.SKIPPED
                    else "❌"
                )
                print(f"    {icon} {s.rule.name}")
            print()

        # Custom rule
        def shout(text: str, rule: RewriteRule, ctx: dict) -> str:
            return text.upper()

        eng.register_handler("shout", shout)
        eng.add_rule(RewriteRule(name="shout", op=RewriteOp.CUSTOM, priority=99))
        r = eng.rewrite("hello world", rule_names=["shout"])
        print(f"  Custom SHOUT: {r.final}")

        print(f"\nStats: {json.dumps(eng.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(eng.stats(), indent=2))


if __name__ == "__main__":
    main()
