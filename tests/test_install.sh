#!/usr/bin/env bash
# Installer tests. Nothing here touches your real hub, home folder settings
# or Codex configuration: everything happens in a temporary folder, and
# CODEX_HOME points into it. No network access and no AI calls.
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export CODEX_HOME="$WORK/codex-home"
mkdir -p "$WORK/old-notes" "$WORK/my old notes" "$CODEX_HOME"
printf 'Lemma. Every widget is ample.\n' > "$WORK/old-notes/notes.tex"
printf 'model = "example"\n' > "$CODEX_HOME/config.toml"
pass() { printf 'ok  %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; [ -f "$WORK/log" ] && tail -30 "$WORK/log"; exit 1; }

# 1. Unattended install from the local copy of the kit.
HUB_YES=1 HUB_DIR="$WORK/hub1" HUB_OWNER="Test Owner" \
HUB_PROJECT_TITLE="Widgets and ampleness" HUB_CONVENTIONS="Base field: C" \
HUB_ARCHIVES="$WORK/old-notes" HUB_ASSISTANTS=claude HUB_INSTALL_TOOLS=no HUB_GIT=no \
  bash "$KIT/install.sh" > "$WORK/log" 2>&1 || fail "unattended install"
(
  cd "$WORK/hub1"
  python3 tools/hub.py check > /dev/null
  python3 tools/hub.py search "widget ample" --project widgets-and-ampleness --archives | grep -q "notes.tex"
  grep -q "Base field: C" projects/widgets-and-ampleness/AGENTS.md
  grep -q "Widgets and ampleness" START_HERE.md
  [ "$(grep -c "Test Owner" START_HERE.md)" = 1 ]          # owner named once on the front page
  grep -q "Import earlier work" projects/widgets-and-ampleness/indexes/CATALOG.md
  grep -q "Import earlier work" PROMPTS.md                  # the section the empty catalog points to
  python3 -c 'import json; json.load(open(".claude/settings.json"))'
  python3 tools/test_hub.py > "$WORK/unit.log" 2>&1 || { cat "$WORK/unit.log"; exit 1; }
) || fail "unattended install produced a broken hub"
[ "$(cat "$CODEX_HOME/config.toml")" = 'model = "example"' ] || fail "Claude-only install changed the Codex settings"
pass "unattended install, hub check, archive search, tool self-tests"

# 1b. Claude Code permissions: the hub's own commands pre-approved, uploading blocked.
python3 - "$WORK/hub1/.claude/settings.json" <<'PYEOF' || fail "Claude Code permissions"
import json, sys
perms = json.load(open(sys.argv[1]))["permissions"]
assert "Bash(python3 tools/hub.py *)" in perms["allow"]
assert "Bash(git push *)" in perms["deny"] and "Edit(/projects/*/catalog/**)" in perms["deny"]
PYEOF
pass "Claude Code permissions: hub commands pre-approved, git push blocked"

# 1c. An install that stops halfway leaves nothing behind, and a rerun works.
if _HUB_TEST_FAIL_DURING_BUILD=1 HUB_YES=1 HUB_DIR="$WORK/hub-retry" HUB_ASSISTANTS=claude \
   HUB_INSTALL_TOOLS=no HUB_GIT=no bash "$KIT/install.sh" > "$WORK/log" 2>&1; then
  fail "simulated failure did not stop the installer"
fi
grep -q "no hub was created" "$WORK/log" || fail "no explanation after a failed install"
[ ! -e "$WORK/hub-retry" ] || fail "a failed install left a hub folder"
! ls -A "$WORK" | grep -q "installing" || fail "a failed install left its staging folder"
HUB_YES=1 HUB_DIR="$WORK/hub-retry" HUB_ASSISTANTS=claude HUB_INSTALL_TOOLS=no HUB_GIT=no \
  bash "$KIT/install.sh" > "$WORK/log" 2>&1 || fail "rerun after a failed install"
(cd "$WORK/hub-retry" && python3 tools/hub.py check > /dev/null) || fail "rerun produced a broken hub"
pass "interrupted install leaves nothing behind; rerun succeeds"

# 2. The 'curl ... | bash' path: script on standard input, kit downloaded as a tarball.
tar -czf "$WORK/kit.tar.gz" -C "$(dirname "$KIT")" --exclude .git --exclude __pycache__ "$(basename "$KIT")"
cat "$KIT/install.sh" | KIT_URL="file://$WORK/kit.tar.gz" HUB_YES=1 HUB_DIR="$WORK/hub2" \
  HUB_PROJECT_TITLE="Piped install" HUB_ASSISTANTS=both HUB_INSTALL_TOOLS=no HUB_GIT=yes \
  HUB_CODEX_TRUST=yes bash > "$WORK/log" 2>&1 || fail "piped install"
grep -q "Downloading the hub kit" "$WORK/log" || fail "piped install did not download the kit"
(cd "$WORK/hub2" && python3 tools/hub.py check > /dev/null && git log --oneline | grep -q "Create research hub") \
  || fail "piped install produced a broken hub"
pass "piped install (curl | bash path) with git history"

# 3. Codex trust entry: added once, earlier settings kept and backed up.
HUB2_REAL="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$WORK/hub2")"
grep -q "^model = \"example\"" "$CODEX_HOME/config.toml" || fail "existing Codex settings were lost"
[ "$(grep -c "\[projects.\"$HUB2_REAL\"\]" "$CODEX_HOME/config.toml")" = 1 ] || fail "trust entry missing or duplicated"
[ -f "$CODEX_HOME/config.toml.before-research-hub" ] || fail "no backup of the Codex settings"
python3 - "$CODEX_HOME/config.toml" <<'PYEOF' || fail "Codex settings are no longer valid TOML"
import sys
try:
    import tomllib
except ImportError:
    sys.exit(0)  # Python < 3.11: skip the parse check
with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)
assert any(v.get("trust_level") == "trusted" for v in data["projects"].values())
PYEOF
pass "Codex trust entry added once, backup kept, TOML valid"

# 4. A person at the keyboard: interactive answers through a real terminal.
if command -v expect > /dev/null; then
  expect "$KIT/tests/interactive.exp" "$KIT/install.sh" "file://$WORK/kit.tar.gz" \
    "$WORK/hub3" "$WORK/my old notes" > "$WORK/log" 2>&1 || fail "interactive install"
  P="$WORK/hub3/projects/hodge-classes-on-abelian-fourfolds"
  grep -q "HC-R001" "$P/AGENTS.md" || fail "lowercase prefix was not upper-cased"
  grep -q "^- abelian varieties are principally polarized$" "$P/AGENTS.md" || fail "conventions not recorded"
  grep -q "Not a folder: ~/no-such-folder-for-this-test" "$WORK/log" || fail "missing archive folder not reported"
  grep -q '"my-old-notes"' "$WORK/hub3/hub.json" || fail "dragged-in folder (with a space) not registered"
  grep -q "DOWN ARROW" "$WORK/log" || fail "closing steps do not explain the trust screen"
  pass "interactive install through a terminal, folder dragged in from Finder"
else
  printf 'skip interactive test (install "expect" to run it)\n'
fi

echo "All install tests passed."
