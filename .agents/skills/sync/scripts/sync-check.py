#!/usr/bin/env python3
"""Static post-sync checks for the graphify fork.

Verifies that a rebase/merge of feat_lang_godot onto upstream kept every
intentional Godot change and did not silently absorb a stale hunk. Compares
against the upstream ref itself where possible, so the checks do not rot as
upstream grows its extension sets or bumps its version.

Usage:
    .venv/Scripts/python .agents/skills/sync/scripts/sync-check.py [repo_root] [--upstream upstream/v8]

Launch it with the project interpreter, never `uv run`. Any `uv run` invocation
without `--frozen` re-syncs the project and rewrites uv.lock's version to match
pyproject.toml, which pulls the lock away from upstream's and trips the version
check on a tree that was in fact merged correctly.

Exit status is 1 if any check fails. Runs no tests; stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

GODOT_EXTENSIONS = {".gd", ".tscn", ".tres"}

failures: list[str] = []
skipped: list[str] = []


def record(ok: bool, name: str, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" - {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)
    return ok


def skip(name: str, why: str) -> None:
    print(f"  SKIP  {name} — {why}")
    skipped.append(name)


def git(root: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else None


def read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def set_literal(source: str, name: str) -> set[str] | None:
    """Parse a one-line `NAME = {...}` set literal out of *source*."""
    match = re.search(rf"^{name} = (\{{.*?\}})$", source, re.MULTILINE)
    if not match:
        return None
    try:
        value = ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, set) else None


def project_version(pyproject: str) -> str | None:
    match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    return match.group(1) if match else None


def check_no_conflict_markers(root: Path) -> None:
    out = git(root, "grep", "-n", "-E", r"^(<{7}|={7}|>{7})( |$)", "--", ".")
    record(not out, "no leftover conflict markers", (out or "").strip().splitlines()[:3] and out.strip().splitlines()[0] or "")


def check_extensions(root: Path, upstream: str) -> None:
    local = read(root, "graphify/detect.py")
    code = set_literal(local, "CODE_EXTENSIONS")
    docs = set_literal(local, "DOC_EXTENSIONS")

    if code is None or docs is None:
        record(False, "detect.py extension sets parse", "CODE_EXTENSIONS/DOC_EXTENSIONS not found as one-line set literals")
        return

    missing = GODOT_EXTENSIONS - code
    record(not missing, "CODE_EXTENSIONS keeps Godot extensions", f"missing {sorted(missing)}")

    ref = git(root, "show", f"{upstream}:graphify/detect.py")
    if ref is None:
        skip("detect.py sets match upstream", f"{upstream} not available — fetch upstream first")
        return

    up_code = set_literal(ref, "CODE_EXTENSIONS")
    up_docs = set_literal(ref, "DOC_EXTENSIONS")
    if up_code is None or up_docs is None:
        skip("detect.py sets match upstream", "could not parse upstream sets")
        return

    lost = up_code - code
    record(not lost, "CODE_EXTENSIONS keeps every upstream extension", f"dropped {sorted(lost)}")
    extra = code - up_code - GODOT_EXTENSIONS
    record(not extra, "CODE_EXTENSIONS adds nothing beyond Godot", f"unexpected {sorted(extra)}")
    record(docs == up_docs, "DOC_EXTENSIONS matches upstream exactly", f"differs by {sorted(docs ^ up_docs)}")


def check_dispatch(root: Path) -> None:
    source = read(root, "graphify/extract.py")
    record(
        "from graphify.extractors.godot import" in source,
        "extract.py imports the Godot extractors",
    )
    for ext, func in ((".gd", "extract_gdscript"), (".tscn", "extract_godot_scene"), (".tres", "extract_godot_scene")):
        record(
            re.search(rf'"{re.escape(ext)}": {func},', source) is not None,
            f"_DISPATCH routes {ext}",
        )
    record(
        'pop("target_file"' not in source,
        "extract.py has no redundant target_file pop",
        "the branch's stale pop survived; resolution.py owns this now",
    )


def check_target_file_routing(root: Path) -> None:
    source = read(root, "graphify/extractors/resolution.py")
    match = re.search(r"target_file_key = \((.*?)\n\s*\)", source, re.DOTALL)
    if not match:
        record(False, "resolution.py keeps the Godot target_file routing", "target_file_key block is gone")
        return
    block = match.group(1)
    record(
        'edge["target_file"]' not in block and 'edge.get("target_file")' not in block,
        "target_file routing reads the popped local, not the edge dict",
        "dead code: upstream pops target_file earlier in this loop",
    )
    record("str(target_file)" in block, "target_file routing uses the popped variable")


def lock_version(lock: str) -> str | None:
    match = re.search(r'\[\[package\]\]\nname = "graphifyy"\nversion = "([^"]+)"', lock)
    return match.group(1) if match else None


def check_version(root: Path, upstream: str) -> None:
    """Both versions are compared against upstream, never against each other.

    Upstream ships pyproject.toml and uv.lock out of step — 0.9.34 bumped the
    project version without re-locking — so pyproject == uv.lock is not an
    invariant the fork can hold. What a stale branch-side hunk looks like is
    divergence from upstream's own pair.
    """
    local_version = project_version(read(root, "pyproject.toml"))
    local_lock = lock_version(read(root, "uv.lock"))

    ref_pyproject = git(root, "show", f"{upstream}:pyproject.toml")
    ref_lock = git(root, "show", f"{upstream}:uv.lock")
    if ref_pyproject is None or ref_lock is None:
        skip("version matches upstream", f"{upstream} not available")
        skip("uv.lock version matches upstream", f"{upstream} not available")
        return
    record(
        local_version == project_version(ref_pyproject),
        "version matches upstream",
        f"local {local_version} vs upstream {project_version(ref_pyproject)}",
    )
    record(
        local_lock is not None and local_lock == lock_version(ref_lock),
        "uv.lock version matches upstream",
        f"local {local_lock} vs upstream {lock_version(ref_lock)}",
    )


def all_extra(pyproject: str) -> set[str] | None:
    match = re.search(r"^all = \[(.*?)\]$", pyproject, re.MULTILINE | re.DOTALL)
    return set(re.findall(r'"([^"]+)"', match.group(1))) if match else None


def check_godot_extra(root: Path, upstream: str) -> None:
    pyproject = read(root, "pyproject.toml")
    record('godot = ["tree-sitter-language-pack"]' in pyproject, "pyproject keeps the godot extra")

    local = all_extra(pyproject)
    if local is None:
        record(False, "the all extra parses", "no `all = [...]` line found")
        return
    record("tree-sitter-language-pack" in local, "the all extra includes tree-sitter-language-pack")

    ref = git(root, "show", f"{upstream}:pyproject.toml")
    if ref is None:
        skip("the all extra keeps upstream's pins", f"{upstream} not available")
        return
    up = all_extra(ref)
    if up is None:
        skip("the all extra keeps upstream's pins", "could not parse upstream's all extra")
        return
    # Upstream retightens pins here (mcp, starlette) and the branch's copy of the
    # line predates them, so resolving this conflict by taking the Godot side
    # reverts every pin upstream has moved. No test notices.
    lost = up - local
    record(not lost, "the all extra keeps upstream's pins", f"reverted to stale {sorted(lost)}")


def lock_extras(lock: str) -> set[str] | None:
    match = re.search(r"^provides-extras = \[(.*?)\]$", lock, re.MULTILINE | re.DOTALL)
    return set(re.findall(r'"([^"]+)"', match.group(1))) if match else None


GODOT_LOCK_EXTRA = 'godot = [\n    { name = "tree-sitter-language-pack" },\n]'
GODOT_LOCK_MARKER = '{ name = "tree-sitter-language-pack", marker = "extra == \'godot\'" }'


def check_lock_godot_extra(root: Path, upstream: str) -> None:
    """uv.lock's own extras metadata is the fork's blind spot.

    Nothing in the repo reads provides-extras and no test touches it, so
    resolving uv.lock by taking the upstream side wholesale drops the godot
    extra with every static check and the whole suite still green. The first
    thing that notices is `uv sync --extra godot`, at install time.
    """
    lock = read(root, "uv.lock")

    extras = lock_extras(lock)
    if extras is None:
        record(False, "uv.lock provides-extras parses", "no `provides-extras = [...]` line found")
    else:
        record("godot" in extras, "uv.lock advertises the godot extra", "provides-extras dropped it")

    record(GODOT_LOCK_EXTRA in lock, "uv.lock keeps the godot extra's package list")
    record(GODOT_LOCK_MARKER in lock, "uv.lock keeps the godot requires-dist marker")

    ref = git(root, "show", f"{upstream}:uv.lock")
    if ref is None or extras is None:
        skip("uv.lock provides-extras keeps upstream's", f"{upstream} not available")
        return
    up = lock_extras(ref)
    if up is None:
        skip("uv.lock provides-extras keeps upstream's", "could not parse upstream's list")
        return
    lost = up - extras
    record(not lost, "uv.lock provides-extras keeps upstream's", f"dropped {sorted(lost)}")


def check_skillgen(root: Path) -> None:
    gen = read(root, "tools/skillgen/gen.py")
    record("def _is_code_exts_line(" in gen, "skillgen keeps the code_exts predicate")
    sanctioned = re.search(r"_SANCTIONED_MONOLITH_DIFFS = \((.*?)\n\)", gen, re.DOTALL)
    record(
        sanctioned is not None and "_is_code_exts_line" in sanctioned.group(1),
        "the code_exts predicate is registered as sanctioned",
    )

    fragment = root / "tools/skillgen/fragments/references/shared/update.md"
    if not fragment.exists():
        skip("update fragment lists Godot extensions", "fragment not found")
        return
    text = fragment.read_text(encoding="utf-8")
    missing = [e for e in sorted(GODOT_EXTENSIONS) if f"'{e}'" not in text and f'"{e}"' not in text]
    record(not missing, "update fragment lists Godot extensions", f"missing {missing}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", nargs="?", default=".")
    parser.add_argument("--upstream", default="upstream/v8")
    args = parser.parse_args()

    root = Path(args.repo_root).resolve()
    if not (root / "graphify" / "detect.py").exists():
        print(f"not a graphify checkout: {root}", file=sys.stderr)
        return 2

    print(f"checking {root} against {args.upstream}\n")
    for section, fn in (
        ("merge hygiene", lambda: check_no_conflict_markers(root)),
        ("detect.py extensions", lambda: check_extensions(root, args.upstream)),
        ("extractor dispatch", lambda: check_dispatch(root)),
        ("target_file routing", lambda: check_target_file_routing(root)),
        ("version", lambda: check_version(root, args.upstream)),
        ("godot extra", lambda: check_godot_extra(root, args.upstream)),
        ("uv.lock godot extra", lambda: check_lock_godot_extra(root, args.upstream)),
        ("skillgen", lambda: check_skillgen(root)),
    ):
        print(section)
        fn()
        print()

    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed" + (f" ({len(skipped)} skipped)" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
