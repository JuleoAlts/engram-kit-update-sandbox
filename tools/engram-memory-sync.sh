#!/usr/bin/env bash
# Keeps the shared Engram memory of one project in step with this repository.
#
#   tools/engram-memory-sync.sh down   bring in what others added (git pull, then engram sync --import)
#   tools/engram-memory-sync.sh up     down, then export what is new, clean it, commit and push
#
# Several developers share at the same time. Every chunk is its own file named by its content, so the only
# thing that can conflict is the index (.engram/manifest.json); tools/merge_manifest.py resolves that by
# keeping the chunks of both sides, and a rejected push is retried once after pulling.
#
# It never fails its caller: every problem is reported with a warning and the script exits 0, so a git
# hook that runs it can never block a code push or a pull. A refusal of the cleaning script means the
# memory is NOT shared; nothing else is affected. It never waits for a password, and it never waits long:
# ssh is told not to prompt and to give up connecting after 15 s, git drops a connection that has gone quiet,
# and every network git call and every `engram sync` is ended by `timeout` (or `gtimeout`) when one is installed.
#
# ENGRAM_MEMORY_DRY_RUN=1 stops `up` before the commit: nothing is committed or pushed.
# ENGRAM_MEMORY_TIMEOUT=<seconds> changes the limit of each of those calls (90 by default; 0 means no limit).
# ENGRAM_MEMORY_PYTHON=<path to python> chooses the Python (otherwise python3, python and py -3 are tried, each one run).
# ENGRAM_MEMORY_LOCK_WAIT=<seconds> (Windows only) how long a `down` waits for a lock in use (30 by default; 0 means no wait).
set -u
export GIT_TERMINAL_PROMPT=0
# Engram must not look for its own updates in the middle of a hook: this kit says when a version is stable (tools.txt of the
# project). It only skips that one call to GitHub: `sync` and `sync --import` do the same with it. The filter of its notice
# in the summary line below stays, for a version of Engram that does not know this switch.
export ENGRAM_NO_UPDATE_CHECK=1

MEM="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$MEM/tools/engram-memory-sync.sh"
SELF_SUM="$(cksum < "$SELF" 2>/dev/null || true)" # to notice when a pull replaces this very script
ACTION="${1:-}"
say() { printf '[engram-memory] %s\n' "$*" >&2; }

# Windows only (Git for Windows, where bash reports msys or cygwin; macOS and Linux never enter this block):
# - A hook that was given up on is gone, and nobody reads what this script says: the first line it writes would kill it
#   with SIGPIPE, maybe before the import. It goes on without its reader.
# - The installer that puts Git's usr/bin on the PATH puts it after System32, whose `timeout`, `find` and `sort` are
#   other programs: `timeout 90 git pull` only prints a syntax error, and `sort -u` lists no chunks.
# - There is no pgrep to find what a command started, and `ps` has the same answer.
ENGRAM_ON_WINDOWS=""
case "${OSTYPE:-}" in msys* | cygwin*) ENGRAM_ON_WINDOWS=1 ;; esac
if [ -n "$ENGRAM_ON_WINDOWS" ]; then
  trap '' PIPE
  case "$(command -v timeout) $(command -v find) $(command -v sort)" in
    */[Ss][Yy][Ss][Tt][Ee][Mm]32/*) PATH="/usr/bin:$PATH"; export PATH ;;
  esac
  if ! command -v pgrep >/dev/null 2>&1; then
    pgrep() { ps -ef 2>/dev/null | awk -v parent="$2" '$3 == parent { print $2 }'; }
  fi
fi

# Time limit of the calls that can hang. Without `timeout` or `gtimeout` (macOS has neither by default) they run as
# before: the ssh and low-speed settings still end a dead connection, only a stalled `engram sync` has no limit.
TIMEOUT_CMD=""
for candidate in timeout gtimeout; do
  if command -v "$candidate" >/dev/null 2>&1; then TIMEOUT_CMD="$candidate"; break; fi
done
LIMIT="${ENGRAM_MEMORY_TIMEOUT:-90}"
case "$LIMIT" in '' | *[!0-9]*) LIMIT=90 ;; esac
# An expired limit ends the command with status 124.
#
# The command runs in the background and the script WAITS for it (`wait` can be interrupted, a foreground command
# cannot): bash does not act on a signal until the foreground command ends, so a sync that is asked to end (the hook
# of Claude Code gives up after 60 seconds) would go on waiting for an Engram that hangs, and leave it running.
# RUN_IN=<folder> runs it from that folder (`RUN_IN="$MEM" run_limited engram sync --import`); NO_LIMIT=1 skips the
# time limit but keeps the rest (the cleaning step is local work whose length depends on the memory, not on a network).
# The command keeps the standard input of the script (`<&0`): a shell gives a background command /dev/null otherwise.
CHILD=""
SIGNALLED=""
run_limited() {
  local limiter="$TIMEOUT_CMD" status
  [ -z "${NO_LIMIT:-}" ] || limiter=""
  # A request to end that arrives before the pid of the command is known is held, then served: otherwise the handler
  # would find no command to end, free the lock and leave it running.
  SIGNALLED=""
  trap 'SIGNALLED=1' TERM INT HUP
  if [ -n "${RUN_IN:-}" ]; then
    if [ -n "$limiter" ]; then ( cd "$RUN_IN" && exec "$limiter" "$LIMIT" "$@" ) <&0 & else ( cd "$RUN_IN" && exec "$@" ) <&0 & fi
  else
    if [ -n "$limiter" ]; then "$limiter" "$LIMIT" "$@" <&0 & else "$@" <&0 & fi
  fi
  CHILD=$!
  trap stop_requested TERM INT HUP
  [ -z "$SIGNALLED" ] || stop_requested
  wait "$CHILD"
  status=$?
  CHILD=""
  return "$status"
}
# The processes below $1, the deepest first (none on a machine without pgrep: then only the command itself is ended).
descendants() {
  local child
  command -v pgrep >/dev/null 2>&1 || return 0
  for child in $(pgrep -P "$1" 2>/dev/null); do
    descendants "$child"
    printf '%s\n' "$child"
  done
}
# Asked to end (TERM, INT or HUP): end the command being waited for and what it started (a `timeout` that stands in
# between would otherwise die and leave an Engram that ignores the request running), force them if they ignore it,
# release the lock, and leave with the status of a process that was ended by a signal.
stop_requested() {
  trap '' TERM INT HUP
  if [ -n "$CHILD" ]; then
    local tree
    tree="$(descendants "$CHILD")" # before anything ends: a process whose parent is gone cannot be found from it
    # shellcheck disable=SC2086
    kill "$CHILD" $tree 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$CHILD" 2>/dev/null || break; sleep 0.2; done
    # shellcheck disable=SC2086
    kill -9 "$CHILD" $tree 2>/dev/null
    wait "$CHILD" 2>/dev/null
  fi
  rm -rf "${LOCK:?}" 2>/dev/null
  exit 143
}
# Every git call that goes over the network: a connection that moves less than 1000 bytes/s for 20 s is dropped (HTTPS).
net_git() { run_limited git -C "$MEM" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=20 "$@"; }
PUSH_NOTE=""

# GIT_TERMINAL_PROMPT only covers HTTPS: ssh could still ask for a passphrase or a host key on the terminal, and it
# could wait a long time for a host that does not answer. So ssh is told never to prompt and to give up connecting.
# The command that git would have used stays the one used, in git's own order of precedence: GIT_SSH_COMMAND, then
# core.sshCommand, then GIT_SSH, then plain ssh; the options go after it, and ssh keeps the first value it finds of an
# option, so anything the user put in the command wins. A client that has no `-o` (PuTTY's plink) is left alone.
ssh_base="${GIT_SSH_COMMAND:-}"
case "$ssh_base" in
  *" -o BatchMode=yes -o ConnectTimeout=15") ;; # already added: this is the restarted copy of the script
  *)
    if [ -z "$ssh_base" ]; then ssh_base="$(git -C "$MEM" config --get core.sshCommand 2>/dev/null || true)"; fi
    if [ -z "$ssh_base" ] && [ -n "${GIT_SSH:-}" ]; then
      ssh_base="'$(printf '%s' "$GIT_SSH" | sed "s/'/'\\\\''/g")'" # quoted: the path may hold spaces
    fi
    [ -n "$ssh_base" ] || ssh_base=ssh
    case "$ssh_base $(git -C "$MEM" config --get ssh.variant 2>/dev/null || true)" in
      *[Pp][Ll][Ii][Nn][Kk]* | *[Pp][Uu][Tt][Tt][Yy]*) ;;
      *) export GIT_SSH_COMMAND="$ssh_base -o BatchMode=yes -o ConnectTimeout=15" ;;
    esac
    ;;
esac

command -v engram >/dev/null 2>&1 || { say "Engram is not installed: skipping."; exit 0; }
[ -f "$MEM/.engram/config.json" ] || { say "no .engram/config.json in $MEM: skipping."; exit 0; }
# Which Python: each candidate is run, because on Windows `python3` is usually the shortcut to the Microsoft Store.
[ -f "$MEM/tools/find-python.sh" ] || { say "tools/find-python.sh is missing from $MEM (an incomplete checkout or an old copy of the tools): skipping. Run git pull there."; exit 0; }
# shellcheck source=find-python.sh
. "$MEM/tools/find-python.sh"
engram_find_python || { say "$(engram_python_missing_message) Nothing was imported or shared."; exit 0; }
PROJECT="$("${PYTHON[@]}" -I -c 'import json,sys; sys.stdout.reconfigure(encoding="utf-8"); print(json.load(open(sys.argv[1], encoding="utf-8-sig"))["project_name"])' "$MEM/.engram/config.json" 2>/dev/null)"
[ -n "$PROJECT" ] || { say "cannot read the project name: skipping."; exit 0; }

# One run at a time. A lock left by a process that is gone is removed.
LOCK="$MEM/.engram/.sync.lock"
# Windows only: a session that resumes fires two SessionStart hooks seconds apart, and the first one is often given up
# on. If the second stepped aside, nothing would import what the first did not reach. So a `down` waits for a lock in
# use, for ENGRAM_MEMORY_LOCK_WAIT seconds (30 by default, well inside the 60 of the hook), and then decides as always.
lock_in_use() {
  local holder
  holder="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -z "$holder" ]; then [ -n "$(find "$LOCK" -maxdepth 0 -mmin -1 2>/dev/null)" ]; return; fi
  kill -0 "$holder" 2>/dev/null && [ -z "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]
}
if [ -n "$ENGRAM_ON_WINDOWS" ] && [ "$ACTION" = down ]; then
  LOCK_WAIT="${ENGRAM_MEMORY_LOCK_WAIT:-30}"
  case "$LOCK_WAIT" in '' | *[!0-9]*) LOCK_WAIT=30 ;; esac
  while [ "$LOCK_WAIT" -gt 0 ] && [ -d "$LOCK" ] && lock_in_use; do
    sleep 1
    LOCK_WAIT=$((LOCK_WAIT - 1))
  done
fi
if ! mkdir "$LOCK" 2>/dev/null; then
  OTHER="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -z "$OTHER" ]; then
    # The owner makes the folder and then writes its pid: a lock younger than a minute with no pid is one being taken.
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin -1 2>/dev/null)" ]; then say "another sync is starting: skipping."; exit 0; fi
  # A sync takes seconds: a lock older than an hour is a dead one, even if its process number belongs to someone else now.
  elif kill -0 "$OTHER" 2>/dev/null && [ -z "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
    say "another sync is running: skipping."; exit 0
  fi
  rm -rf "${LOCK:?}" && mkdir "$LOCK" 2>/dev/null || { say "cannot take the lock: skipping."; exit 0; }
fi
chmod 700 "$LOCK" 2>/dev/null || true # the import output is kept here
echo "$$" > "$LOCK/pid"
trap 'rm -rf "${LOCK:?}"' EXIT
trap stop_requested TERM INT HUP

PATTERNS="$HOME/.config/engram-memory-tools/patterns.json"

manifest_ids() {
  "${PYTHON[@]}" -I -c 'import json,sys; print(" ".join(sorted(c["id"] for c in json.load(open(sys.argv[1], encoding="utf-8-sig"))["chunks"])))' "$MEM/.engram/manifest.json" 2>/dev/null
}

# A chunk this machine created is already part of this machine's memory. Importing it back would overwrite the
# full local observations with the cleaned copy, so its id is recorded as already imported. (Other machines import
# it normally.) Returns 1 when the record could not be written (database locked for longer than
# ENGRAM_MEMORY_DB_TIMEOUT seconds, 10 by default, or a schema that is not the expected one); it says nothing itself.
# With no Engram database on this machine there is no local memory to protect: nothing to record, and it returns 0.
mark_imported() {
  local db="${ENGRAM_DATA_DIR:-$HOME/.engram}/engram.db"
  [ -f "$db" ] || return 0
  NO_LIMIT=1 run_limited "${PYTHON[@]}" -I - "$db" "${ENGRAM_MEMORY_DB_TIMEOUT:-10}" "$@" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    wait = float(sys.argv[2])
except ValueError:
    wait = 10.0
db = sqlite3.connect(sys.argv[1], timeout=wait)
for cid in sys.argv[3:]:
    db.execute("INSERT OR IGNORE INTO sync_chunks(target_key, chunk_id, imported_at) VALUES ('local', ?, datetime('now'))", (cid,))
db.commit()
db.close()
PY
}

# Ids of the chunks that this machine made and has not published: the chunk files under .engram/chunks that are
# untracked or changed since HEAD (a run that stopped before committing, a dry run), plus those that commits not
# published yet added. "Published" means reachable from the upstream branch; a branch with no upstream (or one whose
# remote branch is gone) is compared with every remote-tracking branch instead, so a commit that no remote has
# counts as unpublished. A chunk that arrived with a pull is in the upstream and is not listed: it imports normally.
chunks_made_here_and_not_published() {
  local file
  {
    git -C "$MEM" ls-files --others --exclude-standard -- .engram/chunks 2>/dev/null
    git -C "$MEM" diff --name-only --no-renames --diff-filter=AM HEAD -- .engram/chunks 2>/dev/null
    if git -C "$MEM" rev-parse --verify -q '@{u}' >/dev/null 2>&1; then
      git -C "$MEM" log --name-only --no-renames --diff-filter=AM --pretty=format: '@{u}..HEAD' -- .engram/chunks 2>/dev/null
    else
      git -C "$MEM" log --name-only --no-renames --diff-filter=AM --pretty=format: HEAD --not --remotes -- .engram/chunks 2>/dev/null
    fi
  } | while IFS= read -r file; do
    case "$file" in
      *.jsonl.gz) file="${file##*/}"; file="${file%.jsonl.gz}"
        case "$file" in '' | *[!A-Za-z0-9_-]*) ;; *) printf '%s\n' "$file" ;; esac ;;
    esac
  done | sort -u
}

# The pull may have replaced THIS script. The copy that is running is the old one: it would go on with the old
# logic, and whatever the new version configures would only appear on the next pull. So start the new one, once
# (the variable stops a loop). The lock is released first: the new process takes it again.
reexec_if_updated() {
  if [ "${ENGRAM_MEMORY_REEXEC:-}" = 1 ]; then return 0; fi
  if [ "$(cksum < "$SELF" 2>/dev/null || true)" = "$SELF_SUM" ]; then return 0; fi
  say "the sync script was updated by this pull: running the new one."
  rm -rf "${LOCK:?}"
  trap - EXIT TERM INT HUP
  ENGRAM_MEMORY_REEXEC=1 exec bash "$SELF" "$ACTION"
}

# Uncommitted changes under .engram: a chunk made by a previous run that was not shared yet.
dirty() { [ -n "$(git -C "$MEM" status --porcelain -- .engram 2>/dev/null)" ]; }

# Bring in the commits of the others. Only the manifest can conflict: resolve it and go on.
update_from_remote() {
  local rc
  git -C "$MEM" remote get-url origin >/dev/null 2>&1 || return 0
  net_git pull --rebase --quiet 2>/dev/null; rc=$?
  [ "$rc" = 0 ] && return 0
  if [ -d "$MEM/.git/rebase-merge" ] || [ -d "$MEM/.git/rebase-apply" ]; then
    if "${PYTHON[@]}" -I "$MEM/tools/merge_manifest.py" "$MEM" >/dev/null 2>&1 \
      && git -C "$MEM" add .engram/manifest.json \
      && GIT_EDITOR=true git -C "$MEM" rebase --continue >/dev/null 2>&1; then
      return 0
    fi
    git -C "$MEM" rebase --abort >/dev/null 2>&1
  fi
  if [ "$rc" = 124 ]; then
    say "git pull timed out after ${LIMIT}s: using what is already on disk."
  else
    say "git pull failed (offline, no access, or diverged): using what is already on disk."
  fi
  return 1
}

# Configuration is done here, when the memory comes in: nobody sets anything up by hand.
# A machine with no git identity (a fresh cloud session) cannot commit: its memory commits are signed with a
# neutral name, only in this repository's local config, and an identity that exists is never replaced.
ensure_git_identity() {
  if [ -n "$(git -C "$MEM" config user.email 2>/dev/null)" ]; then return 0; fi
  if [ -n "${GIT_AUTHOR_EMAIL:-}${EMAIL:-}" ]; then return 0; fi
  git -C "$MEM" config user.name "Engram memory"
  git -C "$MEM" config user.email "engram-memory@users.noreply.invalid"
  say "no git identity on this machine: the memory commits are signed 'Engram memory' (only in $MEM)."
}

# The privacy file lists what must be removed from YOUR memory before it is shared; it is built from this
# machine's identity and from the repository itself (see tools/setup.sh). Returns 1 if it could not be made.
ensure_privacy_file() {
  if [ -f "$PATTERNS" ]; then return 0; fi
  bash "$MEM/tools/setup.sh" --auto >&2 2>&1 || true
  [ -f "$PATTERNS" ]
}

import_all() {
  local out rc ids
  # Whatever this machine has not published yet is its own memory: record it as imported BEFORE every import, not
  # only right after cleaning it, so that a mark lost by an interrupted run is made up by the next one. The import
  # cannot take only some chunks, so when the record cannot be written it is skipped altogether until the next run.
  ids="$(chunks_made_here_and_not_published)"
  # shellcheck disable=SC2086
  if [ -n "$ids" ] && ! mark_imported $ids; then
    say "cannot record this machine's own chunks as imported (the Engram database is busy or different): not importing now, so they do not overwrite the local memory. The next run tries again."
    return 0
  fi
  # to a file, not through $(...): a command substitution is a foreground subshell that a signal would have to wait for.
  # The file lives in the lock folder, which this run owns and removes however it ends (a temporary file could not be
  # made on a machine whose temporary folder is full, and would be left behind when the run is ended).
  out_file="$LOCK/import.out"
  RUN_IN="$MEM" run_limited engram sync --import > "$out_file" 2>&1; rc=$?
  out="$(cat "$out_file" 2>/dev/null)"; rm -f "${out_file:?}"
  if [ "$rc" != 0 ]; then
    if [ "$rc" = 124 ]; then say "engram sync --import timed out after ${LIMIT}s: continuing."; else say "engram sync --import failed: continuing."; fi
    return 0
  fi
  # The line to show is the import result ("Imported N new chunk(s)..." or "No new chunks to import."). Engram may print its own
  # update notice before it, in any install flavour (brew, go install, a releases link): so look for the result line first,
  # and only when there is none fall back to the first line that is not part of that notice.
  summary="$(printf '%s\n' "$out" | grep -i -m1 'chunk')"
  [ -n "$summary" ] || summary="$(printf '%s\n' "$out" | grep -v -i -e 'update available' -e 'to update' -e 'brew ' -e 'go install' -e 'releases/latest' -e '^[[:space:]]*or:' | grep -v '^[[:space:]]*$' | head -1)"
  say "memory of $PROJECT: $summary"
}

# Clean what the export (or a previous run) left under .engram, and commit it. Returns 1 if there is nothing to
# share or the cleaning step refused.
clean_and_commit() {
  local before after new id rc
  before="$(manifest_ids)"
  NO_LIMIT=1 run_limited "${PYTHON[@]}" -I "$MEM/tools/sanitize_chunks.py" "$MEM" --only-new > "$MEM/.engram/.sanitize.log" 2>&1; rc=$?
  if [ "$rc" != 0 ]; then
    say "STOP: the cleaning step refused to continue, so NOTHING was pushed. Details: $MEM/.engram/.sanitize.log"
    return 1
  fi
  after="$(manifest_ids)"; new=""
  for id in $after; do case " $before " in *" $id "*) ;; *) new="$new $id" ;; esac; done
  # Best effort (a failure does not stop the sharing), but never silent. import_all records the chunks that are not
  # published yet before it imports, which would make up for a lost mark here only until this very run pushes the
  # chunk: from then on it is no longer "unpublished", this mark is the only protection against a later down
  # importing the cleaned copy over the full local observations, and the next run will not repeat it.
  # shellcheck disable=SC2086
  [ -n "$new" ] && { mark_imported $new || say "could not record the new chunk as imported: the next down may re-import it."; }
  git -C "$MEM" add -A .engram
  if git -C "$MEM" diff --cached --quiet -- .engram; then return 1; fi
  if [ "${ENGRAM_MEMORY_DRY_RUN:-}" = "1" ]; then
    say "dry run: a new cleaned chunk is ready; it would be committed and pushed."
    git -C "$MEM" reset -q -- .engram  # only what this run staged: the developer may have staged other work
    return 1
  fi
  # only what is under .engram: the developer may have staged other work in this repository, which is not ours to commit
  git -C "$MEM" commit -q -m "chore(memory): update Engram memory of $PROJECT" -- .engram || { say "commit failed."; return 1; }
  return 0
}

push_memory() {
  local rc
  git -C "$MEM" remote get-url origin >/dev/null 2>&1 || return 1
  net_git push --quiet 2>/dev/null; rc=$?
  [ "$rc" = 0 ] && return 0
  # A push that ran out of time is not retried: the network is not answering, and a second wait would only add up.
  if [ "$rc" = 124 ]; then PUSH_NOTE=" (timed out after ${LIMIT}s)"; return 1; fi
  # Someone shared in the meantime: take their commits (merging the manifest) and push once more.
  update_from_remote || return 1
  net_git push --quiet 2>/dev/null; rc=$?
  if [ "$rc" = 124 ]; then PUSH_NOTE=" (timed out after ${LIMIT}s)"; fi
  return "$rc"
}

down() {
  if dirty; then
    say "there is memory not shared yet in $MEM: not pulling now (the next push finishes it)."
  else
    update_from_remote || true
    reexec_if_updated
  fi
  # After the pull, so that a clone older than a fix configures itself with the newest tools.
  ensure_git_identity
  ensure_privacy_file || say "could not create $PATTERNS: nothing will be shared until it exists. Run tools/setup.sh once."
  import_all
}

up() {
  local committed=0 pulled=0 rc
  # 0. Fresh tools first: the configuration below is built with them.
  if ! dirty; then
    update_from_remote || true
    pulled=1
    reexec_if_updated
  fi
  ensure_git_identity
  # 1. A previous run may have left a cleaned chunk that was not committed (a dry run, a refusal that was fixed).
  if dirty; then
    say "finishing the memory left by a previous run."
    clean_and_commit && committed=1
    if dirty; then import_all; return 0; fi
  fi
  # 2. The privacy file lists what must be removed from YOUR memory before it is shared. Make it if it is missing.
  if ! ensure_privacy_file; then
    say "could not create $PATTERNS: nothing was shared. Run tools/setup.sh once."
    import_all
    return 0
  fi
  # 3. Bring in what others shared, then export what is new.
  if [ "$pulled" = 0 ]; then update_from_remote || true; fi
  import_all
  RUN_IN="$MEM" run_limited engram sync --project "$PROJECT" >/dev/null 2>&1; rc=$?
  if [ "$rc" != 0 ]; then
    if [ "$rc" = 124 ]; then say "engram sync timed out after ${LIMIT}s: not exporting."; else say "engram sync failed: not exporting."; fi
    return 0
  fi
  clean_and_commit && committed=1
  if dirty; then return 0; fi
  if [ "$committed" = 0 ] && [ -z "$(git -C "$MEM" log '@{u}..HEAD' --oneline 2>/dev/null)" ]; then say "nothing new to share."; return 0; fi
  [ "${ENGRAM_MEMORY_DRY_RUN:-}" = "1" ] && return 0
  if push_memory; then say "memory of $PROJECT shared."; else say "push failed${PUSH_NOTE}: the commit stays local and goes out with the next push."; fi
}

case "${1:-}" in
  down) down ;;
  up) up ;;
  *) say "usage: engram-memory-sync.sh down|up" ;;
esac
exit 0
