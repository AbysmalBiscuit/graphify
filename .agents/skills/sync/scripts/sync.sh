#!/usr/bin/env bash
# Sync this fork: rebase the Godot branch onto the new upstream tip, rebuild the
# integration branch, gate on checks + tests, push both.
#
# Usage: sync.sh [upstream-branch]     defaults to upstream's HEAD, else v8
#
# Worktree-aware and location-independent: run it from any worktree of this
# repo. It finds the main checkout and the worktree holding each branch itself,
# and operates in whichever one owns the branch it needs, so nothing has to be
# checked out anywhere in particular.
#
# Runs the whole happy path unattended and is safe to re-run at any point —
# mid-rebase it continues where it stopped. Stops before anything ambiguous
# (conflicts, dirty tree, failing gate, rejected push) and reports enough state
# for a caller to take over.
#
# Last line of output is always: SYNC-RESULT: <STATUS>
#   PUSHED         rebased, merged, gated, pushed
#   UP-TO-DATE     already on the upstream tip and the fork matches, nothing done
#   CONFLICT       rebase stopped on conflicts, rebase still IN PROGRESS
#   DIRTY          uncommitted changes, nothing done
#   IN-PROGRESS    a merge/cherry-pick was already running, nothing done
#   CHECK-FAILED   the static post-merge check failed, nothing pushed
#   TESTS-FAILED   a test failed beyond the known pre-existing set, nothing pushed
#   REJECTED       origin moved, the lease refused the push
#   PUSH-FAILED    push failed for another reason (network, auth)
#   ERROR          preflight failed (not the fork, missing remote or branch)
set -uo pipefail

FEATURE=${SYNC_FEATURE_BRANCH:-feat_lang_godot}
REMOTE=origin
UPSTREAM=upstream

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/sync-check.py"
CONFLICT_DOC="$(cd "$SCRIPT_DIR/.." && pwd)/references/sync-conflicts.md"

# Failing at the upstream tip on Windows, unrelated to the sync. Verified against
# a clean upstream worktree, not assumed — re-verify before extending this list.
KNOWN_FAILURES=(
  "tests/test_detect.py::test_graphifyignore_matches_nfc_path_with_nfd_pattern"
  "tests/test_detect.py::test_graphifyignore_matches_nfd_path_with_nfc_pattern"
)

REGRESSION_TESTS=(
  tests/test_extract.py
  tests/test_detect.py
  tests/test_skillgen.py
  tests/test_cross_extension_reexport_self_cycle.py
  tests/test_js_import_resolution.py
  tests/test_import_extension_resolution.py
  tests/test_symbol_resolution.py
)

say() { printf '%s\n' "$*" | tr '\r' '\n'; }
finish() { say "SYNC-RESULT: $1"; exit "${2:-0}"; }

main_worktree() {
  git worktree list --porcelain | awk '/^worktree /{print substr($0,10); exit}'
}

# Path of the worktree that has $1 checked out, empty if none does.
worktree_for() {
  git worktree list --porcelain |
    awk -v want="refs/heads/$1" '
      /^worktree /{wt = substr($0, 10)}
      /^branch /{if (substr($0, 8) == want) {print wt; exit}}'
}

# Path of the worktree holding a stopped rebase of $1, empty if none does.
# A stopped rebase detaches HEAD, so `worktree_for` reports the branch as
# checked out nowhere; without this the feature worktree resolves to the main
# checkout and the script starts a second rebase there, on top of the first.
rebasing_worktree_for() {
  local wt gitdir state
  while read -r wt; do
    gitdir=$(git -C "$wt" rev-parse --git-dir 2>/dev/null) || continue
    for state in rebase-merge rebase-apply; do
      [[ -f "$gitdir/$state/head-name" ]] || continue
      if [[ "$(<"$gitdir/$state/head-name")" == "refs/heads/$1" ]]; then
        printf '%s\n' "$wt"
        return
      fi
    done
  done < <(git worktree list --porcelain | awk '/^worktree /{print substr($0, 10)}')
}

report_conflict() {
  local wt=$1
  say ""
  say "conflicted files (in $wt):"
  git -C "$wt" diff --name-only --diff-filter=U | sed 's/^/  /'
  say ""
  say "stopped while applying: $(git -C "$wt" log --oneline -1 REBASE_HEAD 2>/dev/null)"
  local todo
  todo="$(git -C "$wt" rev-parse --git-dir)/rebase-merge/git-rebase-todo"
  if [[ -f "$todo" ]]; then
    say "still queued after this one: $(grep -cvE '^[[:space:]]*(#|$)' "$todo") commit(s)"
  fi
  say ""
  say "resolution recipes: $CONFLICT_DOC"
  say ""
  say "the rebase is IN PROGRESS — resolve, 'git add', then re-run this script"
  say "(it continues the rebase for you). 'git rebase --abort' backs the whole thing out."
  finish CONFLICT 10
}

git rev-parse --git-dir >/dev/null 2>&1 || {
  say "not inside a git repository: $PWD"
  finish ERROR 3
}

MAIN_WT=$(main_worktree)
[[ -n "$MAIN_WT" && -f "$MAIN_WT/graphify/detect.py" ]] || {
  say "main checkout is not a graphify checkout: ${MAIN_WT:-<unknown>}"
  finish ERROR 3
}
say "main checkout: $MAIN_WT"

git -C "$MAIN_WT" remote get-url "$UPSTREAM" >/dev/null 2>&1 || {
  say "no '$UPSTREAM' remote — this script syncs a fork, not a standalone clone"
  git -C "$MAIN_WT" remote -v
  finish ERROR 3
}

# Resolve the upstream branch before anything else — it names the integration
# branch too, and both worktree lookups depend on it.
base=${1:-}
if [[ -z "$base" ]]; then
  base=$(git -C "$MAIN_WT" symbolic-ref --quiet --short "refs/remotes/$UPSTREAM/HEAD" 2>/dev/null)
  base=${base#"$UPSTREAM"/}
  [[ -z "$base" ]] && base=v8
fi
base=${base#"$UPSTREAM"/}
UP_REF="$UPSTREAM/$base"
INTEGRATION="$base"

FEATURE_WT=$(worktree_for "$FEATURE")
INT_WT=$(worktree_for "$INTEGRATION")
[[ -z "$FEATURE_WT" ]] && FEATURE_WT=$(rebasing_worktree_for "$FEATURE")
[[ -z "$FEATURE_WT" ]] && FEATURE_WT=$MAIN_WT
[[ -z "$INT_WT" ]] && INT_WT=$MAIN_WT
say "$FEATURE: $FEATURE_WT"
say "$INTEGRATION: $INT_WT"

# A stopped rebase leaves its worktree detached, so this precedes every other
# state check. Only the feature worktree ever hosts one.
feature_git_dir=$(git -C "$FEATURE_WT" rev-parse --git-dir)
if [[ -e "$feature_git_dir/rebase-merge" || -e "$feature_git_dir/rebase-apply" ]]; then
  if [[ -n "$(git -C "$FEATURE_WT" diff --name-only --diff-filter=U)" ]]; then
    say ""
    say "== rebase already in progress, conflicts still unresolved =="
    git -C "$FEATURE_WT" status --short --branch
    report_conflict "$FEATURE_WT"
  fi
  say ""
  say "== continuing the in-progress rebase =="
  if ! cont_out=$(cd "$FEATURE_WT" && GIT_EDITOR=true git rebase --continue 2>&1); then
    say "$cont_out"
    [[ -n "$(git -C "$FEATURE_WT" diff --name-only --diff-filter=U)" ]] && report_conflict "$FEATURE_WT"
    say ""
    say "rebase --continue failed and left no conflicts to resolve — inspect above"
    finish ERROR 3
  fi
  say "$cont_out"
fi

for wt in "$FEATURE_WT" "$INT_WT"; do
  wt_git_dir=$(git -C "$wt" rev-parse --git-dir)
  for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD; do
    if [[ -e "$wt_git_dir/$marker" ]]; then
      say "an operation is already in progress in $wt ($marker) — nothing done"
      say ""
      git -C "$wt" status --short --branch
      finish IN-PROGRESS 4
    fi
  done
  if [[ -n "$(git -C "$wt" status --porcelain --untracked-files=no)" ]]; then
    say "working tree has uncommitted changes: $wt"
    say ""
    git -C "$wt" status --short --untracked-files=no
    say ""
    say "commit or stash them, then re-run"
    finish DIRTY 5
  fi
done

say ""
say "== fetch =="
git -C "$MAIN_WT" fetch --prune "$UPSTREAM" 2>&1 | tail -10
git -C "$MAIN_WT" fetch --prune "$REMOTE" 2>&1 | tail -10

git -C "$MAIN_WT" rev-parse --verify --quiet "refs/remotes/$UP_REF" >/dev/null || {
  say "no such upstream branch: $UP_REF"
  finish ERROR 3
}
git -C "$MAIN_WT" rev-parse --verify --quiet "refs/heads/$FEATURE" >/dev/null || {
  say "no local feature branch: $FEATURE"
  finish ERROR 3
}

say ""
say "== plan =="
say "upstream tip:  $(git -C "$MAIN_WT" log --oneline -1 "$UP_REF")"
say "feature tip:   $(git -C "$MAIN_WT" log --oneline -1 "$FEATURE")"
say "integration:   $INTEGRATION -> rebuilt as $UP_REF + merge of $FEATURE"

if git -C "$MAIN_WT" merge-base --is-ancestor "$UP_REF" "$FEATURE" 2>/dev/null; then
  say "$FEATURE already sits on the upstream tip — no rebase needed"
else
  say ""
  say "== rebase $FEATURE onto $UP_REF =="
  say "commits to replay:"
  git -C "$MAIN_WT" log --oneline "$UP_REF..$FEATURE" | sed 's/^/  /'
  if ! switch_out=$(git -C "$FEATURE_WT" switch "$FEATURE" 2>&1); then
    say "$switch_out"
    say ""
    say "cannot check out $FEATURE in $FEATURE_WT — rebasing from whatever HEAD"
    say "is there would replay the wrong commits. Resolve the state above first."
    finish ERROR 3
  fi
  if ! rebase_out=$(git -C "$FEATURE_WT" rebase "$UP_REF" 2>&1); then
    say "$rebase_out"
    report_conflict "$FEATURE_WT"
  fi
  say "$rebase_out"
fi

if [[ "$(git -C "$INT_WT" rev-parse -q --verify "$INTEGRATION^1" 2>/dev/null)" == "$(git -C "$INT_WT" rev-parse "$UP_REF")" ]] &&
   [[ "$(git -C "$INT_WT" rev-parse -q --verify "$INTEGRATION^2" 2>/dev/null)" == "$(git -C "$INT_WT" rev-parse "$FEATURE")" ]] &&
   [[ "$(git -C "$INT_WT" rev-parse -q --verify "refs/remotes/$REMOTE/$INTEGRATION")" == "$(git -C "$INT_WT" rev-parse "$INTEGRATION")" ]] &&
   [[ "$(git -C "$INT_WT" rev-parse -q --verify "refs/remotes/$REMOTE/$FEATURE")" == "$(git -C "$INT_WT" rev-parse "$FEATURE")" ]]; then
  say ""
  say "$INTEGRATION is already $UP_REF + a merge of $FEATURE, and $REMOTE matches both"
  finish UP-TO-DATE 0
fi

say ""
say "== rebuild $INTEGRATION in $INT_WT =="
git -C "$INT_WT" switch "$INTEGRATION" >/dev/null 2>&1 ||
  git -C "$INT_WT" switch -c "$INTEGRATION" "$UP_REF" || finish ERROR 3
git -C "$INT_WT" reset --hard "$UP_REF" >/dev/null || finish ERROR 3
if ! merge_out=$(git -C "$INT_WT" merge --no-ff "$FEATURE" -m "merge branch '$FEATURE' into $INTEGRATION" 2>&1); then
  say "$merge_out"
  say ""
  say "the merge conflicted, which means the rebase did not finish — it should be"
  say "a fast-forward of the just-rebased commits. Inspect before resolving."
  finish ERROR 3
fi
say "$merge_out"

say ""
say "== static checks =="
if [[ -x "$INT_WT/.venv/Scripts/python.exe" ]]; then
  PY="$INT_WT/.venv/Scripts/python.exe"
elif [[ -x "$INT_WT/.venv/bin/python" ]]; then
  PY="$INT_WT/.venv/bin/python"
elif [[ -x "$MAIN_WT/.venv/Scripts/python.exe" ]]; then
  PY="$MAIN_WT/.venv/Scripts/python.exe"
elif [[ -x "$MAIN_WT/.venv/bin/python" ]]; then
  PY="$MAIN_WT/.venv/bin/python"
else
  PY=""
fi
if [[ -z "$PY" ]]; then
  say "no project venv found — skipping static checks (run 'uv sync' to enable them)"
elif [[ ! -f "$CHECK_SCRIPT" ]]; then
  say "check script missing: $CHECK_SCRIPT — skipping static checks"
else
  # Deliberately not 'uv run': that re-syncs and rewrites uv.lock's version to
  # match pyproject, pulling the lock away from the upstream copy the check
  # compares it against.
  if ! check_out=$("$PY" "$CHECK_SCRIPT" "$INT_WT" --upstream "$UP_REF" 2>&1); then
    say "$check_out"
    say ""
    say "the merge kept something it should not have, or dropped something it should"
    say "have. Fix per check: $CONFLICT_DOC"
    finish CHECK-FAILED 11
  fi
  say "$check_out" | tail -3
fi

say ""
say "== tests: godot =="
# --frozen throughout the gate: upstream carries a uv.lock whose version trails
# pyproject.toml, so an unfrozen 'uv run' rewrites the lock and leaves both
# worktrees dirty, which the next sync refuses to start on.
if ! godot_out=$(cd "$INT_WT" && uv run --frozen pytest tests/test_godot.py -q 2>&1); then
  say "$godot_out" | tail -25
  say ""
  say "the Godot suite must be fully green — a failure here is the sync's fault"
  finish TESTS-FAILED 12
fi
say "$godot_out" | tail -1

say ""
say "== tests: regression =="
reg_out=$(cd "$INT_WT" && uv run --frozen pytest "${REGRESSION_TESTS[@]}" -q 2>&1)
say "$reg_out" | tail -1
failed=$(printf '%s\n' "$reg_out" | grep -E '^FAILED ' | awk '{print $2}' | tr '\\' '/' | sort -u)
new_failures=""
while IFS= read -r nodeid; do
  [[ -z "$nodeid" ]] && continue
  known=no
  for k in "${KNOWN_FAILURES[@]}"; do
    [[ "$nodeid" == "$k" ]] && known=yes && break
  done
  [[ "$known" == no ]] && new_failures+="  $nodeid"$'\n'
done <<< "$failed"

if [[ -n "$failed" ]]; then
  say ""
  say "failures (known pre-existing on Windows at the upstream tip):"
  printf '%s\n' "$failed" | sed 's/^/  /'
fi
if [[ -n "$new_failures" ]]; then
  say ""
  say "NEW failures, not in the known pre-existing set:"
  printf '%s' "$new_failures"
  say ""
  say "prove each one pre-exists before dismissing it:"
  say "  git worktree add ../graphify-base-check $UP_REF"
  say "  uv run --frozen --project . pytest ../graphify-base-check/<file> -q -k '<test>'"
  say "  git worktree remove ../graphify-base-check --force"
  finish TESTS-FAILED 12
fi

say ""
say "== push =="
push_cmd=(git -C "$MAIN_WT" push --force-with-lease --force-if-includes "$REMOTE" "$FEATURE" "$INTEGRATION")
say "${push_cmd[*]}"
if push_out=$("${push_cmd[@]}" 2>&1); then
  say "$push_out"
  say ""
  git -C "$INT_WT" status --short --branch
  say "$(git -C "$INT_WT" log --oneline -1 HEAD)"
  finish PUSHED 0
fi

say "$push_out"
say ""
if printf '%s' "$push_out" | grep -qiE 'stale info|force-with-lease|force-if-includes|non-fast-forward|fetch first|rejected'; then
  say "$REMOTE moved since the fetch — the lease refused the push"
  say "inspect 'git log $INTEGRATION..$REMOTE/$INTEGRATION' before overriding"
  finish REJECTED 20
fi
say "push failed — see the output above (network or auth)"
finish PUSH-FAILED 21
