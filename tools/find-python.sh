# Finds a working Python 3.8 or newer for the other tools. It is sourced, not run:
#
#   . "$(dirname "$0")/find-python.sh"
#   engram_find_python || { echo "$(engram_python_missing_message)"; exit 1; }
#   "${PYTHON[@]}" -I script.py
#
# `PYTHON` is an array (a command and its arguments, for example `py -3`). Every candidate is RUN, not just looked
# for: on Windows the name `python3` is usually a shortcut to the Microsoft Store that prints a message and runs
# nothing, while the real interpreter is `python` or `py -3` (the installer from python.org creates no python3.exe).
#
# ENGRAM_MEMORY_PYTHON=<python> is tried first and alone: a path, or a command with arguments such as `py -3`. One
# that does not work is an error, never something to replace silently by another interpreter.
#
# A candidate that hangs must not stop the sync: when the caller has set TIMEOUT_CMD (the sync does, when the machine
# has `timeout` or `gtimeout`), each probe is ended after ENGRAM_MEMORY_PROBE_SECONDS (10 by default).

# What each failed candidate said (its first line), for the message below.
ENGRAM_PYTHON_NOTES=""

# engram_python_works <command> [arguments...]: does it run a Python 3.8 or newer?
engram_python_works() {
  local limit="${ENGRAM_MEMORY_PROBE_SECONDS:-10}" output status
  case "$limit" in '' | *[!0-9]*) limit=10 ;; esac
  if [ -n "${TIMEOUT_CMD:-}" ] && [ "$limit" != 0 ]; then
    output="$("$TIMEOUT_CMD" "$limit" "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>&1)"
  else
    output="$("$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>&1)"
  fi
  status=$?
  if [ "$status" -ne 0 ]; then
    output="${output%%$'\n'*}"
    [ -n "$output" ] || output="(no output, status $status)"
    ENGRAM_PYTHON_NOTES="${ENGRAM_PYTHON_NOTES:+$ENGRAM_PYTHON_NOTES; }$*: ${output:0:100}"
    return 1
  fi
  return 0
}

engram_find_python() {
  PYTHON=()
  ENGRAM_PYTHON_NOTES=""
  if [ -n "${ENGRAM_MEMORY_PYTHON:-}" ]; then
    local words
    if engram_python_works "$ENGRAM_MEMORY_PYTHON"; then PYTHON=("$ENGRAM_MEMORY_PYTHON"); return 0; fi
    # not a path that runs: maybe a command with arguments ("py -3")
    read -r -a words <<< "$ENGRAM_MEMORY_PYTHON"
    if [ "${#words[@]}" -gt 1 ] && engram_python_works "${words[@]}"; then PYTHON=("${words[@]}"); return 0; fi
    return 1
  fi
  if engram_python_works python3; then PYTHON=(python3); return 0; fi
  if engram_python_works python; then PYTHON=(python); return 0; fi
  if engram_python_works py -3; then PYTHON=(py -3); return 0; fi
  return 1
}

# What to tell a person when none works (the caller prints it in its own style).
engram_python_missing_message() {
  local said=""
  [ -z "$ENGRAM_PYTHON_NOTES" ] || said=" What they answered: $ENGRAM_PYTHON_NOTES."
  if [ -n "${ENGRAM_MEMORY_PYTHON:-}" ]; then
    printf 'no working Python 3.8 or newer: ENGRAM_MEMORY_PYTHON=%s does not run one. Fix that path or unset the variable.%s' "$ENGRAM_MEMORY_PYTHON" "$said"
  else
    printf 'no working Python 3.8 or newer was found (tried python3, python and py -3, each one run). Install Python from python.org. On Windows the name python3 is often only a shortcut to the Microsoft Store: turn off the python.exe and python3.exe entries of "App execution aliases" (Settings > Apps > Advanced app settings), or set ENGRAM_MEMORY_PYTHON=<path to python>.%s' "$said"
  fi
}
