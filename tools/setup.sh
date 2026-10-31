#!/usr/bin/env bash
# Creates the local file that tells the cleaning script which identifiers to remove from YOUR memory before
# it is shared (your name, emails, GitHub account, computer username and home folder). The file stays on this
# computer (mode 600) and is never committed.
#
#   tools/setup.sh          asks you, with your git identity as the suggestion
#   tools/setup.sh --auto   asks nothing: uses your git identity and your environment (this is what the sync
#                           does by itself the first time you share). Run the interactive form later to add
#                           more emails, your GitHub account or other identities.
#
# A machine that commits as a bot (a cloud session, CI) or runs as `root` does not know who you are, and its
# identity is NOT used: a bot name or `root` would blank out ordinary words in the shared memory. On such a
# machine, the account and the name are learned from this repository itself, and these variables, if set (the
# cloud environment settings are the place), win over that:
#   ENGRAM_MEMORY_NAME      your full name
#   ENGRAM_MEMORY_EMAILS    your emails, comma separated
#   ENGRAM_MEMORY_ACCOUNT   your GitHub account
#   ENGRAM_MEMORY_OTHER     other accounts or identities that must never appear, comma separated
set -eu

CFG="$HOME/.config/engram-memory-tools/patterns.json"
AUTO=0
[ "${1:-}" = "--auto" ] && AUTO=1
if [ -f "$CFG" ]; then chmod 600 "$CFG" 2>/dev/null || true; echo "Already set up: $CFG"; exit 0; fi

# Which Python, before asking anything: each candidate is run (on Windows `python3` is often the Microsoft Store shortcut).
TOOLS="$(cd "$(dirname "$0")" && pwd)"
[ -f "$TOOLS/find-python.sh" ] || { echo "[engram-memory] $TOOLS/find-python.sh is missing (an incomplete checkout or an old copy of the tools): run git pull in the memory repository." >&2; exit 1; }
# shellcheck source=find-python.sh
. "$TOOLS/find-python.sh"
engram_find_python || { echo "[engram-memory] $(engram_python_missing_message)" >&2; exit 1; }

# An identity that names no one: a bot, a cloud session, a system user.
generic_name() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    '' | claude | anthropic | root | user | ubuntu | runner | node | codespace | vscode | github-actions | 'engram memory' | *'[bot]' | *-bot) return 0 ;;
  esac
  return 1
}
generic_email() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    '' | noreply@* | *@anthropic.com | *'[bot]'*) return 0 ;;
  esac
  return 1
}
from_git() { # from_git <key> <test function> -> the git value, or nothing when it is a generic identity
  local value
  value="$(git config "$1" 2>/dev/null || true)"
  if "$2" "$value"; then value=""; fi
  printf '%s' "$value"
}

MEM_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# What this machine cannot know about the owner, learned from the memory repository it just pulled.
hint_account() { # the owner of the memory repository's address (a URL or an scp-like address), never a local path
  local url rest
  url="$(git -C "$MEM_DIR" config --get remote.origin.url 2>/dev/null || true)"
  case "$url" in
    file://*) return 0 ;;
    *://*)
      # Everything up to the first slash (scheme, credentials, host) is dropped; what follows starts with the owner.
      rest="${url#*://}"
      rest="${rest#*/}"
      ;;
    *@*:*) rest="${url#*:}" ;;
    *) return 0 ;;
  esac
  case "$rest" in */*) printf '%s' "${rest%%/*}" ;; esac
}
hint_name() { # the most frequent author of the memory repository that is not a generic identity
  local count name
  git -C "$MEM_DIR" log -n 300 --format=%an 2>/dev/null | sort | uniq -c | sort -rn | while read -r count name; do
    if ! generic_name "$name"; then
      printf '%s' "$name"
      break
    fi
  done
}

ask() { # ask <prompt> <default> -> answer
  if [ "$AUTO" = 1 ]; then printf '%s' "$2"; return 0; fi
  local a
  read -r -p "$1 [$2]: " a
  printf '%s' "${a:-$2}"
}

SYSTEM_USER="${USER:-}"
if generic_name "$SYSTEM_USER"; then SYSTEM_USER=""; fi

GIT_NAME="$(from_git user.name generic_name)"
GIT_ACCOUNT="$(git config github.user 2>/dev/null || true)"
NAME="$(ask 'Your full name' "${ENGRAM_MEMORY_NAME:-${GIT_NAME:-$(hint_name)}}")"
EMAIL="$(ask 'Your main email' "$(from_git user.email generic_email)")"
EXTRA="$(ask 'Other emails, comma separated (optional)' "${ENGRAM_MEMORY_EMAILS:-}")"
ACCOUNT="$(ask 'Your GitHub account' "${ENGRAM_MEMORY_ACCOUNT:-${GIT_ACCOUNT:-$(hint_account)}}")"
USERNAME="$(ask 'Your computer username' "$SYSTEM_USER")"
HOMEPATH="$(ask 'Your home folder' "$HOME")"
OTHER="$(ask 'Other accounts or identities that must never appear, comma separated (optional)' "${ENGRAM_MEMORY_OTHER:-}")"

# The file is created readable by its owner only, from the first byte: with an ordinary umask it would exist for a
# moment readable by others, and it holds the names and addresses to hide.
umask 077
mkdir -p "$(dirname "$CFG")"
NAME="$NAME" EMAIL="$EMAIL" EXTRA="$EXTRA" ACCOUNT="$ACCOUNT" USERNAME="$USERNAME" HOMEPATH="$HOMEPATH" OTHER="$OTHER" CFG="$CFG" \
"${PYTHON[@]}" -I - <<'PY'
import json, os, re
def items(v): return [x.strip() for x in v.split(",") if x.strip()]
emails = [e for e in [os.environ["EMAIL"].strip(), *items(os.environ["EXTRA"])] if e]
cfg = {
    "personal_email_patterns": [re.escape(e) for e in emails],
    "other_identity_patterns": [re.escape(x) for x in items(os.environ["OTHER"])],
    "account": os.environ["ACCOUNT"].strip() or "no-account-given",
    "person_name": os.environ["NAME"].strip() or "no-name-given",
    "username": os.environ["USERNAME"].strip() or "no-username-given",
    "home_path": os.environ["HOMEPATH"].strip() or os.path.expanduser("~"),
}
open(os.environ["CFG"], "w", encoding="utf-8").write(json.dumps(cfg, indent=2) + "\n")
PY
chmod 600 "$CFG"
if [ "$AUTO" = 1 ]; then
  echo "[engram-memory] created your local privacy file from your git identity ($CFG). Run tools/setup.sh to add more emails, your GitHub account or other identities." >&2
else
  echo "Created $CFG"
fi
