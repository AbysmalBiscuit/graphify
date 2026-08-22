#!/usr/bin/env python3
"""Resolve the fork's recurring sync conflicts, and only those.

The same three files conflict on every upstream release, always the same way:
take upstream's version of one construct, then re-apply one fixed Godot
transform.

    graphify/detect.py  upstream's CODE_EXTENSIONS plus .gd/.tscn/.tres;
                        DOC_EXTENSIONS verbatim
    pyproject.toml      upstream's optional-dependency block, plus the godot
                        extra, plus tree-sitter-language-pack in `all`
    uv.lock             upstream's line with "godot" inserted into
                        provides-extras; the version line verbatim

Nothing here reads the upstream ref. Each conflict region already carries
upstream's text as its HEAD side, so the transforms work off the markers git
left in the working tree.

Anything else is left alone: an unrecognized region means the whole file is
skipped and reported, never half-resolved. Whatever this script stages,
sync-check.py verifies afterwards through independent parsing, so a wrong
transform fails the gate rather than reaching a push.

Usage:
    .venv/Scripts/python .agents/skills/sync/scripts/sync-resolve.py [repo_root]

Exit status is 0 when no conflicts remain, 1 when some are left for a human.
Stdlib only; runs no tests and touches no refs beyond `git add`.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

GODOT_EXTENSIONS = ("'.gd'", "'.tscn'", "'.tres'")
GODOT_PACKAGE = "tree-sitter-language-pack"

CONFLICT = re.compile(
    r"^<{7} [^\n]*\n(?P<head>.*?)^={7}\n(?P<branch>.*?)^>{7} [^\n]*\n",
    re.DOTALL | re.MULTILINE,
)


class Unrecognized(Exception):
    """A conflict region no recipe covers. The file is left for a human."""


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


# --- detect.py --------------------------------------------------------------


def resolve_detect(head: str, branch: str) -> str:
    """Upstream's sets, with the Godot extensions put back into the code one.

    DOC_EXTENSIONS conflicts only because upstream keeps growing it; the branch
    never touched it, so upstream's line is the answer unchanged. Taking the
    branch side there silently drops upstream extensions from detection and no
    test catches it.
    """
    out = []
    for line in head.splitlines(keepends=True):
        if line.startswith("CODE_EXTENSIONS = {"):
            if all(ext in line for ext in GODOT_EXTENSIONS):
                out.append(line)
            elif "'.jl', " in line:
                out.append(line.replace("'.jl', ", "'.jl', " + ", ".join(GODOT_EXTENSIONS) + ", ", 1))
            else:
                raise Unrecognized("CODE_EXTENSIONS has no '.jl' anchor to insert after")
        elif line.startswith("DOC_EXTENSIONS = {") or not line.strip():
            out.append(line)
        else:
            raise Unrecognized(f"unexpected line in detect.py conflict: {line.strip()[:60]}")
    return "".join(out)


# --- pyproject.toml ---------------------------------------------------------


def _add_to_all(line: str) -> str:
    if GODOT_PACKAGE in line:
        return line
    return line.replace("]", f', "{GODOT_PACKAGE}"]', 1) if line.rstrip().endswith("]") else line


def resolve_pyproject(head: str, branch: str) -> str:
    """Upstream's block, then the branch's Godot lines re-applied on top.

    Upstream retightens pins inside `all` (mcp and starlette both moved after
    this branch was cut), so taking the branch side of that line reverts every
    one of them with nothing in the suite noticing. Upstream's line is the base;
    the Godot package is appended to it.
    """
    branch_extra = [
        line for line in branch.splitlines(keepends=True) if line.startswith("godot = [")
    ]
    if len(branch_extra) > 1:
        raise Unrecognized("more than one godot extra on the branch side")

    # The comment block immediately above `godot = [...]` explains the extra and
    # travels with it.
    comment: list[str] = []
    if branch_extra:
        lines = branch.splitlines(keepends=True)
        idx = lines.index(branch_extra[0])
        while idx and lines[idx - 1].lstrip().startswith("#"):
            idx -= 1
            comment.insert(0, lines[idx])

    out: list[str] = []
    placed = False
    for line in head.splitlines(keepends=True):
        if line.startswith("all = ["):
            out.extend(comment)
            out.extend(branch_extra)
            placed = True
            out.append(_add_to_all(line))
        else:
            out.append(line)

    if branch_extra and not placed:
        raise Unrecognized("godot extra on the branch side with no `all = [` line to anchor it")

    # Dev-group entries and anything else the branch adds: keep every branch
    # line upstream does not already carry, but only when it is a Godot one.
    # `all` is excluded because the loop above already rebuilt it from
    # upstream's copy; the branch's own is the stale one being discarded.
    head_lines = set(out)
    for line in branch.splitlines(keepends=True):
        if line in head_lines or line in branch_extra or line in comment:
            continue
        if line.startswith("all = ["):
            continue
        if GODOT_PACKAGE in line:
            out.append(line)
        elif line.strip() and not line.lstrip().startswith("#"):
            raise Unrecognized(f"unrecognized branch line in pyproject: {line.strip()[:60]}")
    return "".join(out)


# --- uv.lock ----------------------------------------------------------------


def resolve_lock(head: str, branch: str) -> str:
    """Upstream's line, with "godot" put back into provides-extras.

    The version line is upstream's verbatim: upstream ships pyproject.toml and
    uv.lock out of step on purpose, so matching pyproject here diverges from
    upstream and re-conflicts next release.
    """
    out = []
    for line in head.splitlines(keepends=True):
        if line.startswith("provides-extras = ["):
            if '"godot"' in line:
                out.append(line)
            elif '"all"' in line:
                out.append(line.replace('"all"', '"godot", "all"', 1))
            else:
                raise Unrecognized('provides-extras has no "all" entry to insert before')
        elif line.startswith("version = ") or not line.strip():
            out.append(line)
        else:
            raise Unrecognized(f"unexpected line in uv.lock conflict: {line.strip()[:60]}")
    return "".join(out)


RESOLVERS = {
    "graphify/detect.py": resolve_detect,
    "pyproject.toml": resolve_pyproject,
    "uv.lock": resolve_lock,
}


def resolve_file(root: Path, rel: str) -> str | None:
    """Rewrite every conflict region in *rel*. Returns a reason on refusal."""
    resolver = RESOLVERS.get(rel)
    if resolver is None:
        return "no recipe for this file"

    path = root / rel
    text = path.read_text(encoding="utf-8")
    if not CONFLICT.search(text):
        return "conflict markers not found"

    try:
        resolved, count = CONFLICT.subn(
            lambda m: resolver(m.group("head"), m.group("branch")), text
        )
    except Unrecognized as exc:
        return str(exc)

    if re.search(r"^(<{7}|={7}|>{7})( |$)", resolved, re.MULTILINE):
        return "markers survived the rewrite"

    path.write_text(resolved, encoding="utf-8", newline="")
    git(root, "add", "--", rel)
    print(f"  resolved  {rel} ({count} region{'s' if count != 1 else ''})")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-resolve the fork's recurring sync conflicts.")
    parser.add_argument("repo_root", nargs="?", default=".")
    args = parser.parse_args()

    root = Path(args.repo_root).resolve()
    if not (root / "graphify" / "detect.py").exists():
        print(f"not a graphify checkout: {root}", file=sys.stderr)
        return 2

    conflicted = [p for p in git(root, "diff", "--name-only", "--diff-filter=U").split("\n") if p]
    if not conflicted:
        print("no conflicts to resolve")
        return 0

    left: list[tuple[str, str]] = []
    for rel in conflicted:
        reason = resolve_file(root, rel)
        if reason:
            left.append((rel, reason))

    if left:
        print(f"\n{len(left)} file(s) need a human:")
        for rel, reason in left:
            print(f"  {rel} - {reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
