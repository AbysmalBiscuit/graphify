# Resolving sync conflicts

## What the resolver already did

`sync-resolve.py` applies the recipes below before you see anything, and `sync.sh` drives the rebase through them. If the sync still stopped, the file it stopped on is one the resolver refused: either no recipe covers it, or a conflict region inside a covered file held a line the recipe does not recognise. It refuses whole files, never half of one, so any file it left still carries its markers.

That makes the recipes below two things at once: what the resolver implements, and what you extend when a new conflict starts recurring. A resolution you make by hand more than once belongs in the script.

## Where the toolchain lives

`.agents/skills/sync/` is tracked on `sync-tooling`, a branch no sync rebases, and is gitignored everywhere else. A rebase checks out each commit's tree, so while the toolchain was tracked in `feat_lang_godot` a replay served whichever version the last applied commit carried: `sync-resolve.py` could be missing because a later commit is the one that adds it, and `sync.sh` could be old enough to predate its own fixes and mistake which worktree holds the branch.

Tracked outside every replayed tree, the files sit in the working tree untouched for the whole rebase. Edit them in place. `scripts/tooling.sh save "<subject>" "<body>"` snapshots them onto the branch, `tooling.sh load` writes the branch back over them, and `git add` cannot reach the path at all, which is the point.

## The rule that resolves almost everything

**For any conflicted hunk, take the upstream side unless that hunk is one of the Godot branch's intentional changes.**

The branch sits on a fixed base. Every other difference in a conflicted hunk is just upstream having moved since that base — stale, not intentional. Taking upstream by default is correct there, and it is why every recurring conflict collapses to "take HEAD, then re-add the one Godot bit."

The conflicts land on the first Godot commit: `graphify/detect.py`, `graphify/extract.py`, `pyproject.toml`, `uv.lock`. Which of them actually conflict varies with what upstream touched since the last sync — `pyproject.toml` conflicts on a rebase but not always on a merge, because the two operations diff against different bases.

## Which side is which

`<<<<<<< HEAD` is **always the upstream side**, in both the rebase and the merge. The labelled side (`>>>>>>> 3c35c1f (feat(extract): Godot support...)` during a rebase, `>>>>>>> feat_lang_godot` during a merge) is **always the Godot commit**.

Do not reason from `--ours`/`--theirs`. They invert between merge and rebase — during a rebase `--ours` is the upstream base being replayed onto, which reads backwards. Read the marker labels instead.

## The branch's intentional changes

Everything `feat_lang_godot` deliberately adds. If a conflicted hunk is not in this list, take upstream verbatim.

| File | Intentional change |
|---|---|
| `graphify/detect.py` | the Godot extensions in `CODE_EXTENSIONS`, inserted after `'.jl'` |
| `graphify/extract.py` | `from graphify.extractors.godot import ...`; one `_DISPATCH` entry per Godot extension |
| `graphify/extractors/godot.py` | entire file (new, never conflicts) |
| `graphify/extractors/__init__.py` | exports the two Godot extractors |
| `graphify/extractors/resolution.py` | the `target_file_key` routing block in `_disambiguate_colliding_node_ids` |
| `pyproject.toml` | `godot = ["tree-sitter-language-pack"]` extra; an unpinned dev entry; the same entry in `all` when upstream carries none |
| `tools/skillgen/gen.py` | `_is_code_exts_line` predicate + its entry in `_SANCTIONED_MONOLITH_DIFFS` |
| skill `update.md` fragments + frozen monoliths | the Godot extensions in the `code_exts` set |
| `tests/test_godot.py`, `tests/fixtures/godot_project/` | new (never conflict) |
| `uv.lock` | the `godot` extra block, its `provides-extras` entry, and its unpinned `requires-dist` route to the grammar package |
| `CHANGELOG.md` | the Godot entry |
| `CLAUDE.md` | entire file (new, never conflicts) |

Note what is **not** on that list: `DOC_EXTENSIONS`, the `graphifyy` version, and the end-of-`extract()` cleanup block. Conflicts there are pure staleness.

## Recipes

### `graphify/detect.py`

Take the HEAD literal for both sets verbatim, then insert the Godot extensions into `CODE_EXTENSIONS` immediately after `'.jl', `.

`scripts/sync_common.py` holds the one list those extensions come from. Both `sync-resolve.py` and `sync-check.py` read it, so adding a Godot file type is a one-line change there and the checker starts demanding it everywhere at once.

`DOC_EXTENSIONS` conflicts only because upstream keeps adding to it (`.skill` landed in 0.9.x). The branch never touched it — take HEAD unchanged. Taking the Godot side here silently drops upstream extensions from detection, and no test catches it.

### `graphify/extract.py`

Take HEAD for the whole cleanup block at the end of `extract()`.

The branch adds a `for e in all_edges: e.pop("target_file", None)` there. It was necessary at the old base and is not now, because `_disambiguate_colliding_node_ids` pops the hint off every edge it consumes, on both its early-exit path and its main loop. Keeping the branch's version also drops upstream's `_callable_class` and `local_alias` pops.

Upstream pops the same key elsewhere, inside its ambiguous-import loop, and that line is upstream's to keep. The check matches only the cleanup form, a pop whose receiver is the loop variable of a `for` over `all_edges`.

### `pyproject.toml`

The conflict is the optional-dependency block. Take **upstream's `all = [...]` line verbatim** — it carries upstream's current pins, which the branch's copy predates (`mcp>=1,<3` and `starlette>=1.3.1,<2` both tightened after the branch was cut) — then append `"tree-sitter-language-pack"` to it unless upstream already carries it, which it does once it pins the pack for extras of its own. Keep the `godot = ["tree-sitter-language-pack"]` extra and its comment from the Godot side; upstream has no such line, so it appears only there. The dev-group entry stays unpinned for the same reason the lock's route does.

Taking the Godot side wholesale silently reverts every upstream pin in `all`, and nothing in the test suite notices.

The `version` line conflicts only sometimes; when it does, take upstream's.

### `uv.lock`

Four region shapes conflict, and which of them appear varies by release:

| Region | Resolution |
|---|---|
| the `graphifyy` version line | upstream's, verbatim |
| `provides-extras` | upstream's list, with `"godot"` inserted before `"all"` |
| the grammar package's `requires-dist` entries | upstream's entries, plus an unpinned `extra == 'godot'` route sorted in by extra name |
| the grammar package's `[[package]]` block | upstream's, verbatim |

The last two are not a choice. A lock resolves one version per package, so when upstream pins `tree-sitter-language-pack` for extras of its own, that pin is the only one that can stand and the fork's extra follows whatever it says. A fork-side floor contradicting it leaves the resolution with no answer, which is why the dev entry carries no specifier.

Do **not** resolve the whole file by picking one side. Upstream's side drops the `godot` extra and its `requires-dist` route; the branch's side reverts whatever upstream has repinned since. Resolve region by region, per the table above.

`provides-extras` is the fork's blind spot, so resolve it deliberately. Nothing in the repo reads it and no test touches it, which means an upstream-side pick drops the `godot` extra with every static check and the full suite still green. `uv sync --extra godot` is the first thing that notices, at install time. `sync-check.py` now asserts it, along with the
`godot = [...]` block and the `extra == 'godot'` requires-dist marker.

Match upstream's lock version, never `pyproject.toml`'s. Upstream ships the two out of step — 0.9.34 bumped the project version and left the lock at 0.9.31 — so an equal-to-pyproject resolution silently diverges from upstream and conflicts again on the next sync.

That divergence is also what `uv` will inflict on you unasked: an unfrozen `uv run` re-syncs the project and rewrites the lock version to match `pyproject.toml`. Hence `--frozen` on every `uv run` in the test gate, and the venv interpreter rather than `uv run` for the checker.

### `graphify/extractors/resolution.py` — no conflict, still wrong

This file auto-merges. The merged result is dead code.

The Godot block computes `target_file_key` from `edge["target_file"]`, but upstream's loop now does `target_file = edge.pop("target_file", None)` a few lines above it. After the pop the dict lookup is always `None`, the block never fires, and `.tscn` → `.gd` edges dangle or self-loop.

Fix: read the popped local.

```python
target_file_key = (
    (edge.get("target", ""), _source_key(str(target_file), root))
    if target_file else None
)
```

Nothing fails loudly and git calls the file merged. One test does catch it — `test_extract_pipeline_unifies_scene_and_script`, via the `player.gd`/`player.tscn` fixture pair — so a broken routing shows up as 22/23 rather than an import error. The static check finds it in a second, before the test gate runs.

## Generalizing the trap

Read every file in the intentional-changes table, not just the conflicted ones. A hunk that auto-merges into code reading state a nearby upstream line now consumes is a silent regression, and git reports it as success.

The specific shape to look for: the branch's code reads a key/attribute/variable that upstream has started popping, consuming, renaming, or moving earlier in the same scope.

## New test failures

`sync.sh` re-runs each failure that is not in `KNOWN_FAILURES` against a scratch worktree at the upstream ref, installed with its own environment so upstream's tests run against upstream's code rather than the branch's. Four verdicts:

- `REGRESSION`: passes upstream, so the merge broke it. Real work.
- `PRE-EXISTING`: fails upstream too. The output prints the `KNOWN_FAILURES` line to paste in. Adding it stays manual, because a test broken both upstream and by the merge reads as pre-existing, and a gate whose exception list grows by itself stops being a gate.
- `BRANCH-ONLY`: the test file does not exist upstream, so it is the branch's own and the merge owns the failure.
- `INCONCLUSIVE`: pytest could not collect it there. Usually a renamed or mistyped node id; classify it by hand.

## Speed

`sync.sh` switches on `rerere` for the repo the first time it runs, so a resolution you have already made replays itself when the same hunk conflicts again. It records resolutions by conflict content, which means it helps within one sync (an aborted-and-retried rebase) far more than across releases: upstream edits these literals, so next release's conflict text is not identical and rerere misses. Cross-release repetition is what `sync-resolve.py` covers.

A hit replays silently, with no conflict and no warning, so a resolution cached back when the recipes were wrong reapplies that error unasked. `sync-check.py` is the only thing that catches it, because it reads the merged result rather than what the resolver meant to write.

## Verify before pushing

`sync.sh` runs this after the merge and stops on `CHECK-FAILED`. To run it by hand mid-rebase:

```bash
.venv/Scripts/python .agents/skills/sync/scripts/sync-check.py     # .venv/bin/python on POSIX
```

Static checks only — every recipe above, plus leftover conflict markers. It does not run tests; the test gate is separate.

Launch it with the venv interpreter, not `uv run` — see the `uv.lock` recipe for why.
