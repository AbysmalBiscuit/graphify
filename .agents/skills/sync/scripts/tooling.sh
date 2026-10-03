#!/usr/bin/env bash
#   tooling.sh save <subject> [body]   working tree -> sync-tooling
#   tooling.sh load                    sync-tooling -> working tree

# The toolchain sits at a gitignored path no sync rebases, so a replay cannot
# swap it out mid-run. The ordinary index cannot reach it either, hence the
# scratch index in `save`.
set -euo pipefail

BRANCH=sync-tooling
PREFIX=.agents/skills/sync

root=$(git rev-parse --show-toplevel)
cmd=${1:-}

die() { printf '%s\n' "$*" >&2; exit 1; }

case "$cmd" in
  load)
    git -C "$root" rev-parse --verify --quiet "$BRANCH" >/dev/null \
      || die "no $BRANCH branch. Fetch it from origin first."
    git -C "$root" restore --source="$BRANCH" --worktree -- "$PREFIX"
    printf 'loaded %s from %s\n' "$PREFIX" "$BRANCH"
    ;;

  save)
    subject=${2:-}
    [ -n "$subject" ] || die "usage: tooling.sh save <subject> [body]"
    [ -d "$root/$PREFIX" ] || die "$PREFIX is not in the working tree. Run 'tooling.sh load'."

    idx="$root/.git-tooling-index"
    rm -f "$idx"
    trap 'rm -f "$idx"' EXIT

    # --force clears the ignore rule that hides the whole path, so the exclude
    # has to put build droppings back out of reach by hand.
    GIT_INDEX_FILE="$idx" git -C "$root" add --force -- \
      "$PREFIX" ":(exclude)$PREFIX/**/__pycache__/**"
    tree=$(GIT_INDEX_FILE="$idx" git -C "$root" write-tree)

    parent=$(git -C "$root" rev-parse --verify --quiet "$BRANCH" || true)
    if [ -n "$parent" ] && [ "$tree" = "$(git -C "$root" rev-parse "$BRANCH^{tree}")" ]; then
      printf 'no change to save\n'
      exit 0
    fi

    msg=$subject
    [ -n "${3:-}" ] && msg=$(printf '%s\n\n%s\n' "$subject" "$3")

    if [ -n "$parent" ]; then
      commit=$(printf '%s' "$msg" | git -C "$root" commit-tree "$tree" -p "$parent" -F -)
      git -C "$root" update-ref "refs/heads/$BRANCH" "$commit" "$parent"
    else
      commit=$(printf '%s' "$msg" | git -C "$root" commit-tree "$tree" -F -)
      git -C "$root" update-ref "refs/heads/$BRANCH" "$commit" ""
    fi
    printf 'saved %s to %s\n' "$(git -C "$root" rev-parse --short "$commit")" "$BRANCH"
    ;;

  *)
    die "usage: tooling.sh save <subject> [body] | tooling.sh load"
    ;;
esac
