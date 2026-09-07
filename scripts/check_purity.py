#!/usr/bin/env python3
"""Purity grep, per SPEC-05 §1 and SPEC-10 Phase 0.

Fails the build if forbidden impure calls appear under engines/ or domain/,
or if strategy vocabulary leaks into the execution agent package (SPEC-04 §8.5).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ENGINE_ROOTS = ["backend/app/engines", "backend/app/domain"]
AGENT_ROOTS = ["agent/agent"]

# Word-boundary patterns for calls that break purity/determinism (P7).
IMPURE_PATTERNS = [
    re.compile(r"\bdatetime\.now\s*\("),
    re.compile(r"\btime\.time\s*\("),
    re.compile(r"\brandom\.\w+\s*\("),
    re.compile(r"\buuid4\s*\("),
    re.compile(r"\bos\.environ\b"),
]

STRATEGY_WORDS = [
    re.compile(r"\batr\b", re.IGNORECASE),
    re.compile(r"\bsignal\b", re.IGNORECASE),
    re.compile(r"\bconfluence\b", re.IGNORECASE),
    re.compile(r"\bstrategy\b", re.IGNORECASE),
]


def scan(root: str, patterns: list[re.Pattern[str]]) -> list[str]:
    violations: list[str] = []
    base = Path(root)
    if not base.exists():
        return violations
    for path in base.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in patterns:
                if pattern.search(line):
                    violations.append(f"{path}:{lineno}: {line.strip()}")
    return violations


def main() -> int:
    violations: list[str] = []
    for root in ENGINE_ROOTS:
        violations.extend(scan(root, IMPURE_PATTERNS))
    for root in AGENT_ROOTS:
        violations.extend(scan(root, STRATEGY_WORDS))

    if violations:
        print("Purity check failed:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    print("Purity check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
