#!/usr/bin/env python3
"""Validate commit messages against the repository release standard."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ALLOWED_TYPES = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)

HEADER_RE = re.compile(
    rf"^({'|'.join(ALLOWED_TYPES)})(\([A-Za-z0-9._/-]+\))?(!)?: .+"
)
MERGE_RE = re.compile(r"^(Merge|Revert) ")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: conventional_commit_msg.py <commit-msg-file>", file=sys.stderr)
        return 2

    message_path = Path(sys.argv[1])
    lines = [
        line.strip()
        for line in message_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    if not lines:
        print("Commit message is empty.", file=sys.stderr)
        return 1

    header = lines[0]
    if MERGE_RE.match(header) or HEADER_RE.match(header):
        return 0

    allowed = ", ".join(f"{commit_type}:" for commit_type in ALLOWED_TYPES)
    print("Commit message must follow Conventional Commits.", file=sys.stderr)
    print(f"Allowed types: {allowed}", file=sys.stderr)
    print("Examples: feat: add module input, fix: correct IAM policy, docs: update usage", file=sys.stderr)
    print("Use type! or a BREAKING CHANGE footer for major releases.", file=sys.stderr)
    print(f"Current header: {header}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
