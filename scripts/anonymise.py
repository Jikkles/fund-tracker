"""
Anonymise funds.json for public hosting.

The desk's audit log and caveats were written conversationally and refer to
the maintainer by first name. That has to go before the repo is public.

The tricky part: the same first name appears legitimately as FUND MANAGER
data - Tom Record (Ninety One alum), Tom Miedema (Walter Scott), Tomasz
Boniek (Troy). Those are public professional facts about the funds and must
survive untouched. A naive find-and-replace would corrupt them.

Strategy: mask protected manager names first, rewrite what remains, then
restore. Fails loudly if a protected name is damaged.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Fund manager names containing the maintainer's first name as a substring.
# These are public professional data and MUST be preserved verbatim.
# Extend this list if a new manager with the same name is added.
PROTECTED_NAMES = [
    "Tomasz Boniek",
    "Tom Record",
    "Tom Miedema",
]

# Ordered rewrites for maintainer references. Longest/most specific first so
# that e.g. "for Tom to confirm" is handled before the bare-name fallback.
REWRITES: list[tuple[str, str]] = [
    (r"\bso Tom can see\b", "so the reader can see"),
    (r"\bso Tom knows\b", "so it is clear"),
    (r"\bTom flagged\b", "a review flagged"),
    (r"\bTom reported\b", "a review reported"),
    (r"\bTom noticed\b", "it was noticed"),
    (r"\bflagged to Tom\b", "flagged for review"),
    (r"\bLeft for Tom to review\b", "Left for manual review"),
    (r"\bfor Tom to review\b", "for manual review"),
    (r"\bfor Tom to confirm\b", "pending manual confirmation"),
    (r"\bpending Tom confirming\b", "pending manual confirmation of"),
    (r"\bTom's\b", "the maintainer's"),
    (r"\bTom\b", "the maintainer"),
]


def anonymise(text: str) -> tuple[str, int]:
    """Return (rewritten_text, replacement_count)."""
    # 1. Mask protected manager names.
    masks: dict[str, str] = {}
    for i, name in enumerate(PROTECTED_NAMES):
        token = f"\x00PROTECTED{i}\x00"
        masks[token] = name
        text = text.replace(name, token)

    # 2. Rewrite maintainer references.
    count = 0
    for pattern, replacement in REWRITES:
        text, n = re.subn(pattern, replacement, text)
        count += n

    # 3. Restore protected names.
    for token, name in masks.items():
        text = text.replace(token, name)

    return text, count


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "data/funds.json")
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src

    raw = src.read_text(encoding="utf-8")
    cleaned, count = anonymise(raw)

    # Integrity checks before writing.
    for name in PROTECTED_NAMES:
        if raw.count(name) != cleaned.count(name):
            print(f"ERROR: protected manager name {name!r} was damaged "
                  f"({raw.count(name)} -> {cleaned.count(name)}). Aborting.",
                  file=sys.stderr)
            return 1

    try:
        json.loads(cleaned)
    except json.JSONDecodeError as exc:
        print(f"ERROR: anonymised output is not valid JSON: {exc}",
              file=sys.stderr)
        return 1

    # Report any surviving bare references, excluding protected names.
    probe = cleaned
    for name in PROTECTED_NAMES:
        probe = probe.replace(name, "")
    leftover = len(re.findall(r"\bTom\b", probe))
    if leftover:
        print(f"WARNING: {leftover} unhandled reference(s) remain - "
              f"add a rule to REWRITES", file=sys.stderr)
        for m in re.finditer(r".{0,70}\bTom\b.{0,70}", probe):
            print(f"    ...{m.group(0)}...", file=sys.stderr)
        return 1

    dst.write_text(cleaned, encoding="utf-8")
    print(f"Anonymised {count} reference(s) -> {dst}")
    print(f"Preserved manager names: {', '.join(PROTECTED_NAMES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
