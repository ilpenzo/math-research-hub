#!/usr/bin/env bash
# math-research-hub installer.
#
#   curl -fsSL https://raw.githubusercontent.com/ilpenzo/math-research-hub/main/install.sh | bash
#
# or, from a downloaded copy of the repository:   bash install.sh
#
# It asks a few questions, then creates one folder (your research hub). It
# changes nothing else on your computer unless you say yes to installing an
# AI assistant or a PDF tool, and it uploads nothing.
#
# Unattended use (testing): HUB_YES=1 accepts defaults; HUB_DIR, HUB_OWNER,
# HUB_PROJECT_TITLE, HUB_PROJECT_SLUG, HUB_PREFIX, HUB_DESCRIPTION,
# HUB_CONVENTIONS, HUB_ARCHIVES (paths separated by ':'), HUB_ASSISTANTS
# (claude, codex, both), HUB_INSTALL_TOOLS, HUB_CODEX_TRUST and HUB_GIT
# (yes/no) override the answers. CODEX_HOME is honoured as Codex does.

set -euo pipefail

KIT_REPO="${KIT_REPO:-ilpenzo/math-research-hub}"
KIT_REF="${KIT_REF:-main}"
KIT_URL="${KIT_URL:-https://github.com/$KIT_REPO/archive/refs/heads/$KIT_REF.tar.gz}"

if [ -t 1 ]; then
  B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
  B=""; D=""; G=""; Y=""; R=""; N=""
fi

say()  { printf '%s\n' "$*"; }
head_() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
note() { printf '%s%s%s\n' "$D" "$*" "$N"; }
warn() { printf '%s%s%s\n' "$Y" "$*" "$N"; }
die()  { printf '\n%sStopped: %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

INTERACTIVE=0
if [ "${HUB_YES:-0}" != "1" ] && ( : < /dev/tty ) 2>/dev/null; then
  INTERACTIVE=1
fi

# ask VAR "Question" "default"
ask() {
  local __var="$1" __q="$2" __def="${3:-}" __ans=""
  if [ "$INTERACTIVE" = "1" ]; then
    if [ -n "$__def" ]; then
      printf '%s%s%s [%s]: ' "$B" "$__q" "$N" "$__def" > /dev/tty
    else
      printf '%s%s%s: ' "$B" "$__q" "$N" > /dev/tty
    fi
    IFS= read -r __ans < /dev/tty || true
  fi
  [ -z "$__ans" ] && __ans="$__def"
  printf -v "$__var" '%s' "$__ans"
}

# ask_yes "Question" default(y/n) -> returns 0 for yes
ask_yes() {
  local __q="$1" __def="$2" __ans=""
  if [ "$INTERACTIVE" = "1" ]; then
    local hint="[y/N]"; [ "$__def" = "y" ] && hint="[Y/n]"
    printf '%s%s%s %s: ' "$B" "$__q" "$N" "$hint" > /dev/tty
    IFS= read -r __ans < /dev/tty || true
  fi
  [ -z "$__ans" ] && __ans="$__def"
  case "$__ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# ask_lines VAR "Question"  (one item per line, empty line ends)
ask_lines() {
  local __var="$1" __q="$2" __line="" __all=""
  if [ "$INTERACTIVE" = "1" ]; then
    printf '%s%s%s\n' "$B" "$__q" "$N" > /dev/tty
    note "(One per line. Press Enter on an empty line to finish.)" > /dev/tty
    while :; do
      printf '  > ' > /dev/tty
      IFS= read -r __line < /dev/tty || break
      [ -z "$__line" ] && break
      __all+="$__line"$'\n'
    done
  fi
  printf -v "$__var" '%s' "$__all"
}

expand_path() {
  case "$1" in
    "~") printf '%s' "$HOME" ;;
    "~/"*) printf '%s/%s' "$HOME" "${1#\~/}" ;;
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$PWD" "$1" ;;
  esac
}

main() {
# ------------------------------------------------------------------ welcome
head_ "Research hub installer"
say "This sets up a folder where AI assistants (Claude Code and/or Codex) can"
say "work on your mathematics under rules that keep the record honest: every"
say "claim labelled with who checked it, citations checked against sources,"
say "failed attempts remembered, and proofs reviewed by a fresh checker."
say ""
say "You will answer about ten questions. Press Enter to accept the answer in"
say "[brackets]. Nothing is uploaded, and nothing outside the new folder is"
say "changed unless you agree to install an assistant or a PDF tool."

# ------------------------------------------------------------------ system checks
head_ "Checking your computer"
OS="$(uname -s)"
case "$OS" in
  Darwin) say "${G}ok${N}  macOS" ;;
  Linux)
    if grep -qi microsoft /proc/version 2>/dev/null; then say "${G}ok${N}  Linux on Windows (WSL)"
    else say "${G}ok${N}  Linux"; fi ;;
  *) die "this installer needs macOS or Linux. On Windows, install WSL first (search \"install WSL\" on learn.microsoft.com), open the Ubuntu terminal, and run the install command there." ;;
esac

PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  if [ "$OS" = "Darwin" ]; then
    die "Python 3.9 or newer is needed. Run   xcode-select --install   (Apple's free developer tools include Python), then run this installer again."
  else
    die "Python 3.9 or newer is needed. On Ubuntu or Debian run   sudo apt install python3   then run this installer again."
  fi
fi
say "${G}ok${N}  $("$PY" --version)"

# ------------------------------------------------------------------ find the kit
KIT=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [ -d "$candidate/template" ] && KIT="$candidate"
fi
if [ -z "$KIT" ]; then
  command -v curl >/dev/null || die "curl is needed to download the kit."
  TMP_KIT="$(mktemp -d)"
  trap 'rm -rf "$TMP_KIT"' EXIT
  say "Downloading the hub kit from github.com/$KIT_REPO ..."
  curl -fsSL "$KIT_URL" \
    | tar -xz -C "$TMP_KIT" --strip-components=1 \
    || die "could not download https://github.com/$KIT_REPO. Check your internet connection."
  KIT="$TMP_KIT"
fi
[ -f "$KIT/template/tools/hub.py" ] || die "the kit at $KIT is incomplete (template/tools/hub.py missing)."

HAVE_CLAUDE=0; command -v claude >/dev/null 2>&1 && HAVE_CLAUDE=1
HAVE_CODEX=0; command -v codex >/dev/null 2>&1 && HAVE_CODEX=1
[ "$HAVE_CLAUDE" = 1 ] && say "${G}ok${N}  Claude Code is installed" || note "--  Claude Code is not installed yet"
[ "$HAVE_CODEX" = 1 ] && say "${G}ok${N}  Codex is installed" || note "--  Codex is not installed yet"
HAVE_PDFTOTEXT=0; command -v pdftotext >/dev/null 2>&1 && HAVE_PDFTOTEXT=1
[ "$HAVE_PDFTOTEXT" = 1 ] && say "${G}ok${N}  pdftotext (lets the assistants search PDFs)" || note "--  pdftotext is not installed (optional, for searching PDFs)"

# ------------------------------------------------------------------ questions
head_ "1. About you"
DEFAULT_OWNER="$(git config --global user.name 2>/dev/null || true)"
if [ -z "$DEFAULT_OWNER" ] && [ "$OS" = "Darwin" ]; then DEFAULT_OWNER="$(id -F 2>/dev/null || true)"; fi
[ -z "$DEFAULT_OWNER" ] && DEFAULT_OWNER="${USER:-Researcher}"
OWNER="${HUB_OWNER:-}"; [ -z "$OWNER" ] && ask OWNER "Your name (used in notes and the changelog)" "$DEFAULT_OWNER"

head_ "2. Where the hub lives"
note "A new folder. Everything the assistants write goes inside it."
while :; do
  HUB_PATH="${HUB_DIR:-}"; [ -z "$HUB_PATH" ] && ask HUB_PATH "Folder for your hub" "$HOME/research-hub"
  HUB_PATH="$(expand_path "$HUB_PATH")"
  if [ -e "$HUB_PATH" ] && [ -n "$(ls -A "$HUB_PATH" 2>/dev/null)" ]; then
    [ "$INTERACTIVE" = "1" ] && [ -z "${HUB_DIR:-}" ] || die "$HUB_PATH already exists and is not empty."
    warn "$HUB_PATH already exists and is not empty. Choose another folder."
    continue
  fi
  break
done

head_ "3. Your first research project"
note "You can add more projects later. Example: \"Rationality of cubic fourfolds\"."
TITLE="${HUB_PROJECT_TITLE:-}"; [ -z "$TITLE" ] && ask TITLE "Project title" "My first project"
DEF_SLUG="$("$PY" -c 'import re,sys; s=re.sub(r"[^a-z0-9]+","-",sys.argv[1].lower()).strip("-")[:40]; print(s if len(s)>=2 else "project")' "$TITLE")"
DEF_PREFIX="$("$PY" -c '
import re, sys
words = [w for w in re.findall(r"[A-Za-z]+", sys.argv[1]) if w.lower() not in {"of","the","a","an","and","on","in","for","to","with"}]
p = "".join(w[0] for w in words).upper()[:4]
if len(p) < 2: p = (re.sub(r"[^A-Za-z]", "", sys.argv[1]).upper() + "XX")[:2]
print(p)' "$TITLE")"
SLUG="${HUB_PROJECT_SLUG:-}"
while :; do
  [ -z "$SLUG" ] && ask SLUG "Short folder name for it (lowercase, hyphens)" "$DEF_SLUG"
  "$PY" -c 'import re,sys; sys.exit(0 if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", sys.argv[1]) else 1)' "$SLUG" && break
  [ "$INTERACTIVE" = "1" ] || die "invalid project folder name '$SLUG'."
  warn "Use lowercase letters, digits and hyphens, at least two characters."; SLUG=""
done
PREFIX="${HUB_PREFIX:-}"
while :; do
  [ -z "$PREFIX" ] && ask PREFIX "Two to five capital letters to prefix its record IDs (e.g. ${DEF_PREFIX}-R001)" "$DEF_PREFIX"
  PREFIX="$(printf '%s' "$PREFIX" | tr '[:lower:]' '[:upper:]')"
  "$PY" -c 'import re,sys; sys.exit(0 if re.fullmatch(r"[A-Z]{2,5}", sys.argv[1]) else 1)' "$PREFIX" && break
  [ "$INTERACTIVE" = "1" ] || die "invalid prefix '$PREFIX'."
  warn "Use two to five letters."; PREFIX=""
done
DESCRIPTION="${HUB_DESCRIPTION:-}"
[ -z "$DESCRIPTION" ] && ask DESCRIPTION "In a sentence or two, the main question (optional; you can add it later)" ""

head_ "4. Conventions the assistants must always state"
note "These go into the project's rules and every helper's brief. Examples:"
note "  the base field and its characteristic; smooth vs. singular; projective vs. proper;"
note "  sheaf vs. vector bundle; which normalization of a constant; finite vs. infinite groups."
CONVENTIONS_RAW="${HUB_CONVENTIONS:-}"
[ -z "$CONVENTIONS_RAW" ] && ask_lines CONVENTIONS_RAW "Conventions (optional)"

head_ "5. Your earlier work (optional)"
note "Folders of notes, TeX files, code or PDFs the assistants may search but never change."
note "Example: ~/Documents/research/old-notes"
ARCHIVES_RAW=""
if [ -n "${HUB_ARCHIVES:-}" ]; then
  ARCHIVES_RAW="$(printf '%s' "$HUB_ARCHIVES" | tr ':' '\n')"$'\n'
else
  ask_lines ARCHIVES_RAW "Folders to register as read-only archives"
fi
ARCHIVES=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  p="$(expand_path "$line")"
  if [ -d "$p" ]; then ARCHIVES+=("$p"); else warn "Skipping $line: not a folder."; fi
done <<< "$ARCHIVES_RAW"

head_ "6. AI assistants"
note "Claude Code (Anthropic) needs a paid Claude plan (Pro, Max, Team or Enterprise) or an API account."
note "Codex (OpenAI) needs a ChatGPT plan that includes Codex (e.g. Plus or Pro) or an API key."
note "The hub works with either; with both, each can review the other's proofs."
if [ "$HAVE_CLAUDE$HAVE_CODEX" = "11" ]; then DEF_AS=3; elif [ "$HAVE_CODEX" = 1 ]; then DEF_AS=2; else DEF_AS=1; fi
ASSISTANTS="${HUB_ASSISTANTS:-}"
if [ -z "$ASSISTANTS" ]; then
  say "  1) Claude Code    2) Codex    3) Both"
  ask choice "Which will you use?" "$DEF_AS"
  case "$choice" in 2) ASSISTANTS=codex ;; 3) ASSISTANTS=both ;; *) ASSISTANTS=claude ;; esac
fi
USE_CLAUDE=0; USE_CODEX=0
case "$ASSISTANTS" in claude) USE_CLAUDE=1 ;; codex) USE_CODEX=1 ;; *) USE_CLAUDE=1; USE_CODEX=1 ;; esac

INSTALL_CLAUDE=0; INSTALL_CODEX=0; INSTALL_POPPLER=0
DEF_TOOLS="${HUB_INSTALL_TOOLS:-}"
yes_tools() { # question default
  if [ -n "$DEF_TOOLS" ]; then [ "$DEF_TOOLS" = "yes" ]; else ask_yes "$1" "$2"; fi
}
if [ "$USE_CLAUDE" = 1 ] && [ "$HAVE_CLAUDE" = 0 ]; then
  yes_tools "Install Claude Code now with Anthropic's official installer?" y && INSTALL_CLAUDE=1
fi
if [ "$USE_CODEX" = 1 ] && [ "$HAVE_CODEX" = 0 ]; then
  yes_tools "Install Codex now with OpenAI's official installer?" y && INSTALL_CODEX=1
fi
if [ "$HAVE_PDFTOTEXT" = 0 ] && [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
  yes_tools "Install pdftotext (Homebrew package 'poppler') so PDFs can be searched?" y && INSTALL_POPPLER=1
fi
TRUST_CODEX=0
if [ "$USE_CODEX" = 1 ]; then
  note "Codex loads a folder's settings and helper roles only after the folder is marked"
  note "as trusted (the same as answering yes to Codex's own \"trust this folder\" question)."
  if [ -n "${HUB_CODEX_TRUST:-}" ]; then [ "$HUB_CODEX_TRUST" = "yes" ] && TRUST_CODEX=1
  elif ask_yes "Mark the new hub as trusted in your Codex settings?" y; then TRUST_CODEX=1; fi
fi

head_ "7. Version history"
USE_GIT=0
if command -v git >/dev/null 2>&1; then
  note "git keeps every past version of the hub on your computer, so any change can be undone."
  note "Nothing is uploaded; the assistants are told never to push anywhere without your instruction."
  if [ -n "${HUB_GIT:-}" ]; then [ "$HUB_GIT" = "yes" ] && USE_GIT=1
  elif ask_yes "Keep a local version history with git?" y; then USE_GIT=1; fi
else
  note "git is not installed, so the hub will rely on its own backups in .hub/history/."
fi

# ------------------------------------------------------------------ confirm
head_ "Ready"
say "  Hub folder:   $HUB_PATH"
say "  Owner:        $OWNER"
say "  Project:      $TITLE  (folder projects/$SLUG, IDs $PREFIX-R001, ...)"
say "  Archives:     ${#ARCHIVES[@]} folder(s)"
say "  Assistants:   $ASSISTANTS"
[ "$INSTALL_CLAUDE$INSTALL_CODEX$INSTALL_POPPLER" != "000" ] && say "  Will install: $([ $INSTALL_CLAUDE = 1 ] && printf 'Claude Code ')$([ $INSTALL_CODEX = 1 ] && printf 'Codex ')$([ $INSTALL_POPPLER = 1 ] && printf 'poppler')"
say "  git history:  $([ $USE_GIT = 1 ] && echo yes || echo no)"
[ "$TRUST_CODEX" = 1 ] && say "  Codex trust:  adds one entry to ${CODEX_HOME:-~/.codex}/config.toml (backed up first)"
if [ "$INTERACTIVE" = "1" ]; then
  ask_yes "Create the hub now?" y || die "nothing was created."
fi

# ------------------------------------------------------------------ build
head_ "Creating your hub"
mkdir -p "$HUB_PATH"
cp -R "$KIT/template/." "$HUB_PATH/"
find "$HUB_PATH" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

CONVENTIONS_MD="$(printf '%s' "$CONVENTIONS_RAW" | sed '/^[[:space:]]*$/d; s/^[[:space:]]*[-*][[:space:]]*//; s/^/- /')"

HUB_PATH="$HUB_PATH" OWNER="$OWNER" "$PY" - <<'PYEOF'
import datetime, json, os
from pathlib import Path
hub = Path(os.environ["HUB_PATH"])
owner = os.environ["OWNER"]
today = datetime.date.today().isoformat()
(hub / "hub.json").write_text(json.dumps({
    "schema_version": 1, "owner": owner, "created": today,
    "kit": "https://github.com/ilpenzo/math-research-hub",
    "archives": {}, "projects": {}}, indent=2) + "\n")
for name in ("START_HERE.md", "CHANGELOG.md"):
    path = hub / name
    text = path.read_text()
    text = text.replace("{{HUB_NAME}}", f"Research hub of {owner}").replace("{{OWNER}}", owner).replace("{{DATE}}", today)
    path.write_text(text)
settings = {
    "permissions": {
        "allow": [f"Bash(python3 tools/hub.py {c} *)" for c in
                  ("context", "search", "show", "impact", "check", "render", "hash", "doctor", "new-note")],
        "deny": ["Edit(/projects/*/catalog/**)", "Edit(/hub.json)", "Edit(/library/**)", "Edit(/.hub/**)"],
    }
}
(hub / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
PYEOF

cd "$HUB_PATH"
"$PY" tools/hub.py add-project "$SLUG" --title "$TITLE" --prefix "$PREFIX" \
  --description "$DESCRIPTION" --conventions "$CONVENTIONS_MD" >/dev/null
say "${G}ok${N}  project $SLUG"
for a in "${ARCHIVES[@]+"${ARCHIVES[@]}"}"; do
  "$PY" tools/hub.py add-archive "$a" --project "$SLUG" >/dev/null && say "${G}ok${N}  archive $a"
done
"$PY" tools/hub.py render >/dev/null
"$PY" tools/hub.py check >/dev/null && say "${G}ok${N}  hub check passed" || warn "The hub check reported a problem; run  python3 tools/hub.py check  to see it."
"$PY" tools/test_hub.py >/dev/null 2>&1 && say "${G}ok${N}  tool self-test passed" || warn "The tool self-test failed; run  python3 tools/test_hub.py  to see why."

if [ "$USE_GIT" = 1 ]; then
  git init -q
  if [ -z "$(git config user.name || true)" ]; then git config user.name "$OWNER"; fi
  if [ -z "$(git config user.email || true)" ]; then git config user.email "${USER:-owner}@localhost"; fi
  git add -A && git commit -qm "Create research hub" && say "${G}ok${N}  local version history started"
fi

if [ "$TRUST_CODEX" = 1 ]; then
  CODEX_CFG="${CODEX_HOME:-$HOME/.codex}/config.toml"
  HUB_PATH="$HUB_PATH" CODEX_CFG="$CODEX_CFG" "$PY" - <<'PYEOF' || warn "Could not update $CODEX_CFG; answer yes when Codex asks whether to trust the folder."
import json, os, shutil
from pathlib import Path
cfg = Path(os.environ["CODEX_CFG"])
hub = os.path.realpath(os.environ["HUB_PATH"])
header = "[projects." + json.dumps(hub) + "]"   # a valid TOML quoted key
text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
if header in text:
    print("ok  Codex already trusts this folder")
else:
    if cfg.exists():
        shutil.copy2(cfg, cfg.with_name("config.toml.before-research-hub"))
    cfg.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
    cfg.write_text(text + sep + header + '\ntrust_level = "trusted"\n', encoding="utf-8")
    print(f"ok  Codex will load the hub's settings (trusted folder added to {cfg})")
PYEOF
fi

# ------------------------------------------------------------------ optional installs
if [ "$INSTALL_CLAUDE" = 1 ]; then
  head_ "Installing Claude Code (official installer from claude.ai)"
  curl -fsSL https://claude.ai/install.sh | bash || warn "Claude Code did not install. See https://code.claude.com/docs/en/setup"
fi
if [ "$INSTALL_CODEX" = 1 ]; then
  head_ "Installing Codex (official installer from chatgpt.com)"
  curl -fsSL https://chatgpt.com/codex/install.sh | sh || warn "Codex did not install. See https://github.com/openai/codex"
fi
if [ "$INSTALL_POPPLER" = 1 ]; then
  head_ "Installing poppler (pdftotext) with Homebrew"
  brew install poppler || warn "poppler did not install; PDF search will be skipped."
fi
if [ "$HAVE_PDFTOTEXT" = 0 ] && [ "$INSTALL_POPPLER" = 0 ] && [ "$OS" = "Linux" ]; then
  note "Optional: for PDF search run   sudo apt install poppler-utils"
fi

# ------------------------------------------------------------------ next steps
head_ "${G}Your hub is ready:${N}${B} $HUB_PATH"
say ""
say "Next steps:"
say "  1. Open a new terminal window (so newly installed programs are found) and type:"
say "       cd \"$HUB_PATH\""
if [ "$USE_CLAUDE" = 1 ]; then
  say "       claude"
  say "     The first time, it opens your browser to log in to your Claude account, then"
  say "     asks whether you trust this folder: answer yes, or it ignores the hub's settings."
fi
if [ "$USE_CODEX" = 1 ]; then
  [ "$USE_CLAUDE" = 1 ] && say "     (or, for Codex:)"
  say "       codex"
  say "     The first time, choose \"Sign in with ChatGPT\". If asked whether to trust"
  say "     this folder, answer yes so it loads the hub's helper roles and settings."
fi
say "  2. Paste this as your first message:"
say "       Read START_HERE.md and AGENTS.md, then give me a ten-line tour of this hub"
say "       and suggest what I should do first."
say "  3. More ready-made requests are in PROMPTS.md inside the hub."
say ""
note "If 'claude' or 'codex' is \"not found\" even in a new window, add this line to ~/.zshrc"
note "(or ~/.bashrc on Linux) and open a new window:   export PATH=\"\$HOME/.local/bin:\$PATH\""
}

# Wrapped in a function so that 'curl ... | bash' reads the whole script first.
main "$@"
