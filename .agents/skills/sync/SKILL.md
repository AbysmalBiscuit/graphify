---
description: Sync the fork with upstream, keeping the Godot branch [sync]
allowed-tools: Bash, Read, Glob, Grep, Edit, Write, TaskCreate, TaskUpdate, TaskList, TaskGet
---

Rebase `feat_lang_godot` onto the new upstream tip, rebuild the integration branch as
`upstream/<base>` + one merge, gate on checks and tests, push both to `origin`.

Takes an optional upstream branch argument; defaults to upstream's HEAD, else `v8`.

## What already happened

`.agents/skills/sync/scripts/sync.sh` has **already run** — fetch, rebase, rebuild, static checks,
test gate, and push with `--force-with-lease --force-if-includes`. Its output is below.
Invoking `/sync` authorizes those force-pushes; both branches are rewritten by design,
and that is the point of the command, not something to second-guess or re-confirm.

The script is worktree-aware. It finds the main checkout and the worktree holding each
branch on its own, and reports all three at the top of its output. Work in whichever
worktree it names — do not assume the branch you need is the one you are standing in.

The last line is `SYNC-RESULT: <STATUS>`. Everything you need about repo state is in
that output. Do not re-run `git status`, `git log`, `git branch -vv`, or `git worktree
list` to orient yourself or to double-check a result the script already reported.

The script is safe to re-run at any point; mid-rebase it continues where it stopped.

---

!`bash .agents/skills/sync/scripts/sync.sh "$ARGUMENTS" 2>&1 || true`

---

## Act on the status

| `SYNC-RESULT` | What to do |
|---|---|
| `PUSHED` | Done. Report the integration tip and that both branches went up, in one line. Stop. |
| `UP-TO-DATE` | Done, nothing to sync. Say so in one line. Stop. |
| `CONFLICT` | The real work — see below. |
| `CHECK-FAILED` | The merge kept a stale hunk or dropped an intentional one. Read the named check in `.agents/skills/sync/references/sync-conflicts.md`, fix it in the integration worktree, commit the fix into the merge (`git commit --amend` if the merge commit is still the tip), re-run the script. |
| `TESTS-FAILED` | Only the *new* failures matter; the script already subtracted the known pre-existing set. Debug them as real breakage, then re-run. Do not extend the known-failures list without proving a failure at the upstream tip first, using the worktree recipe the script printed. |
| `DIRTY` | Uncommitted changes predate the command. Report what's uncommitted and in which worktree, ask whether to commit, stash, or drop. Don't decide for them. |
| `IN-PROGRESS` | A merge or cherry-pick was already running. Report the state and ask how to proceed. |
| `REJECTED` | Someone pushed to `origin` since the fetch. Do not override — show `git log <branch>..origin/<branch>` and ask. |
| `PUSH-FAILED` | Read the network/auth output above, fix the cause, re-run the script. |
| `ERROR` | Read the message, fix the precondition, re-run the script. |

## Resolving a `CONFLICT`

The rebase is **in progress** in the feature worktree the script named, and the
conflicted files are listed above.

**Read `.agents/skills/sync/references/sync-conflicts.md` first.** It carries the rule that resolves
almost every hunk, the side-marker convention, per-file recipes, and an
auto-merging file that is silently wrong. Do not resolve from first principles when a
recipe exists.

1. Resolve each conflicted file per that document. The general rule: take the upstream
   side unless the hunk is one of the branch's intentional changes, which the document
   inventories.
2. Check the files the branch touched that did *not* conflict — a clean auto-merge can
   still be dead code.
3. `git add <files>`, then re-run this command. The script continues the rebase itself;
   more conflicts may follow on the second commit.
4. If two changes are genuinely incompatible and nothing in the repo or the commit
   messages disambiguates, `git rebase --abort` and ask — don't silently pick a side.
