#!/usr/bin/env bash
# Opt-in check that the real assistants load a freshly installed hub: the
# rules, the five helper roles and the paper skill. It makes one short,
# read-only call to each installed assistant using your existing login, so it
# uses a little of your plan. Your real Codex settings are not changed: Codex
# runs with a temporary CODEX_HOME that only links to your login file.
# Both calls get empty standard input, since codex exec otherwise waits for it.
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
REAL_CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
export CODEX_HOME="$WORK/codex-home"
mkdir -p "$CODEX_HOME"
HUB_YES=1 HUB_DIR="$WORK/hub" HUB_PROJECT_TITLE="Live check" HUB_ASSISTANTS=both \
  HUB_INSTALL_TOOLS=no HUB_GIT=no HUB_CODEX_TRUST=yes bash "$KIT/install.sh" > "$WORK/install.log" 2>&1 \
  || { cat "$WORK/install.log"; exit 1; }
cd "$WORK/hub"
ROLES="archive.scout source.checker proof.explorer counterexample.checker proof.checker"
QUESTION="Without running any tools or commands, answer in under 80 words: (1) the helper agent \
types or custom roles you could start, names only, exactly as your tools list them; (2) the skills \
you can see that belong to this folder; (3) the first heading of your project instructions."
status=0
check() { # label output
  local missing=""
  for r in $ROLES; do printf '%s' "$2" | grep -qiE "$r" || missing="$missing $r"; done
  printf '%s' "$2" | grep -qi "math-paper-writing" || missing="$missing math-paper-writing"
  printf '%s' "$2" | grep -qi "Research hub rules" || missing="$missing rules-heading"
  if [ -z "$missing" ]; then printf 'ok   %s sees the rules, all five roles and the paper skill\n' "$1"
  else printf 'FAIL %s did not report:%s\n%s\n' "$1" "$missing" "$2"; status=1; fi
}
ONLY="${LIVE_CHECK_ONLY:-}"   # set to claude or codex to check just one
if [ "$ONLY" != codex ] && command -v claude > /dev/null; then
  check "Claude Code" "$(claude -p --permission-mode plan "$QUESTION" < /dev/null 2>&1 || true)"
else
  echo "skip Claude Code (not selected or not installed)"
fi
if [ "$ONLY" != claude ] && command -v codex > /dev/null && [ -f "$REAL_CODEX_HOME/auth.json" ]; then
  ln -s "$REAL_CODEX_HOME/auth.json" "$CODEX_HOME/auth.json"
  check "Codex" "$(codex exec -s read-only --skip-git-repo-check "$QUESTION" < /dev/null 2>&1 | tail -12 || true)"
else
  echo "skip Codex (not selected, not installed or not logged in)"
fi
exit $status
