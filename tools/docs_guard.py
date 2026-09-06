#!/usr/bin/env python3
# Vendored from ~/Dev/Skillz/docs-sync/scripts/docs_guard.py — do not edit here; docs-sync audit checks this copy against the canonical one.
"""Soft pre-commit guard: warn when staged code touches documented areas but
no doc is staged. Always exits 0 — this is advisory, never a hard block."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def _translate_segment(seg: str) -> str:
    """Translate one path segment's wildcards (no `/` inside it)."""
    out = []
    i = 0
    while i < len(seg):
        if seg[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif seg[i] == "*":
            out.append("[^/]*")
            i += 1
        elif seg[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(seg[i]))
            i += 1
    return "".join(out)


def translate_glob(pattern: str) -> str:
    """Translate one glob/exclude pattern to a regex fragment (no anchors).

    `**` as a standalone path segment matches zero or more full segments,
    gitignore-style: `**/x.py` matches top-level `x.py`, and `a/**/b` matches
    `a/b` as well as `a/x/b`. A `**` fused with other characters in the same
    segment (e.g. `foo**bar`) is not a standalone segment and is translated
    character-by-character, same as a run of `*`.
    """
    segs = pattern.split("/")
    n = len(segs)
    out: list[str] = []
    for i, seg in enumerate(segs):
        if seg == "**":
            if n == 1:
                out.append(".*")
            elif i == 0:
                out.append("(?:.*/)?")
            elif i == n - 1:
                out.append("/.*")
            else:
                out.append("/(?:.*/)?")
            continue
        if i > 0 and segs[i - 1] != "**":
            out.append("/")
        out.append(_translate_segment(seg))
    return "".join(out)


def compile_glob(pattern: str) -> Callable[[str], bool]:
    has_wild = "*" in pattern or "?" in pattern
    if pattern.endswith("/"):
        if has_wild:
            regex = re.compile("^" + translate_glob(pattern))
            return lambda p, _r=regex: bool(_r.match(p))
        return lambda p, _pref=pattern: p.startswith(_pref)
    if has_wild:
        regex = re.compile("^" + translate_glob(pattern) + "$")
        return lambda p, _r=regex: bool(_r.match(p))
    prefix = pattern + "/"
    return lambda p, _pat=pattern, _pref=prefix: p == _pat or p.startswith(_pref)


def parse_covers(index_text: str) -> dict[str, list[str]]:
    covers: dict[str, list[str]] = {}
    marker = " covers: "
    for line in index_text.splitlines():
        if not line.startswith("- `"):
            continue
        rest = line[3:]
        tick = rest.find("`")
        if tick == -1:
            continue
        path, rest = rest[:tick], rest[tick + 1 :]
        # Rightmost marker, not the first: a hand-written scope may itself
        # contain the literal " covers: " substring (or an "anchors:" field
        # may precede covers in either grammar order); the real field is
        # always the trailing one. Same right-to-left approach as
        # docs_sync.parse_doc_line.
        idx = rest.rfind(marker)
        if idx == -1:
            continue
        covers_str = rest[idx + len(marker) :]
        covers[path] = [c.strip() for c in covers_str.split(", ") if c.strip()]
    return covers


CONFIG_START = "<!-- docs-sync"
CONFIG_END = "-->"


def parse_exclude(index_text: str) -> list[str]:
    """Read the `exclude:` list from the `<!-- docs-sync ... -->` config
    block, if present. Minimal by design: docs_guard.py only needs `exclude`
    (to skip staged files under excluded paths), not the full config."""
    lines = index_text.splitlines()
    block_start = None
    for idx, line in enumerate(lines):
        if line.strip().startswith(CONFIG_START):
            block_start = idx
            break
    if block_start is None:
        return []
    for line in lines[block_start + 1 :]:
        s = line.strip()
        if s == CONFIG_END:
            break
        m = re.match(r"^exclude:\s*(.*)$", s)
        if m:
            val = m.group(1).strip()
            if not val:
                return []
            try:
                return shlex.split(val)
            except ValueError:
                # Advisory-only guard: an unparseable config line is a
                # docs-sync audit finding, not something to crash a commit
                # over. Fall back to whitespace-split rather than erroring.
                return val.split()
    return []


WARN_HEADER = "docs-sync: staged code touches documented areas but no doc is staged."
WARN_FOOTER = "  Review those docs (and their docs/INDEX.md line) or commit knowingly."


def is_doc_path(path: str) -> bool:
    """A staged path that counts as 'a doc was touched': anything under docs/
    or a repo-root markdown file."""
    return path.startswith("docs/") or (path.endswith(".md") and "/" not in path)


def main() -> int:
    try:
        repo = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
        index_path = repo / "docs" / "INDEX.md"
        if not index_path.exists():
            return 0
        # -z: without it git quotes non-ASCII paths ("docs/caf\303\251.md").
        staged = [
            s
            for s in subprocess.run(
                ["git", "diff", "--cached", "--name-only", "-z"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split("\0")
            if s
        ]
        index_text = index_path.read_text(encoding="utf-8")
        exclude_matchers = [compile_glob(e) for e in parse_exclude(index_text)]
        staged = [s for s in staged if not any(m(s) for m in exclude_matchers)]

        if any(is_doc_path(s) for s in staged):
            return 0

        covers = parse_covers(index_text)
        hits: dict[str, list[str]] = {}
        for doc, globs in covers.items():
            matchers = [compile_glob(g) for g in globs]
            matched = [s for s in staged if any(m(s) for m in matchers)]
            if matched:
                hits[doc] = matched

        if hits:
            print(WARN_HEADER)
            for doc in sorted(hits):
                print(f"  {doc}  <-  {', '.join(hits[doc][:5])}")
            print(WARN_FOOTER)
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
