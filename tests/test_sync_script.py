"""Integration tests of tools/engram-memory-sync.sh with real git repositories and a fake `engram`.

Run them from the repository root with:  python3 -m unittest discover -s tests

Each test builds a bare "remote", a seed commit with the real tools of this repository, and one clone per
developer. `engram` is a small fake that, when asked to export, writes one RAW chunk (with a prompt in it)
and adds it to the manifest, exactly like the real one does. Nothing here touches the real Engram, the real
configuration or the network.
"""
import gzip
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_sanitize_chunks import write_chunk
from windows_support import WINDOWS, fill_windows_farm, link_command, shell_kill, start_live_process, write_script

REPO = Path(__file__).resolve().parent.parent

FAKE_ENGRAM = r'''#!/usr/bin/env bash
# With FAKE_ENGRAM_ENV_LOG set (a file name) it records its first argument and the update-check switch that it was given.
[ -z "${FAKE_ENGRAM_ENV_LOG:-}" ] || echo "$1 $2 ${ENGRAM_NO_UPDATE_CHECK:-unset}" >> "$FAKE_ENGRAM_ENV_LOG"
case "$1 $2" in
  "sync --import")
    # With FAKE_ENGRAM_SLOW set (a number of seconds) the import takes that long, as one with many chunks.
    [ -z "${FAKE_ENGRAM_SLOW:-}" ] || sleep "$FAKE_ENGRAM_SLOW"
    # With FAKE_ENGRAM_HANG set (a file name) it hangs, as an Engram that waits on a locked database or a dead network.
    # With FAKE_ENGRAM_DEAF also set it ignores the request to end (an ignored signal stays ignored through exec).
    if [ -n "${FAKE_ENGRAM_HANG:-}" ]; then
      [ -z "${FAKE_ENGRAM_DEAF:-}" ] || trap '' TERM
      echo "$$" > "$FAKE_ENGRAM_HANG"; exec sleep 300
    fi
    # With FAKE_ENGRAM_IMPORT_LOG set it behaves like the real one with respect to the database: it imports every
    # chunk of the manifest that sync_chunks does not list yet, records it there, and logs what it did.
    if [ -n "${FAKE_ENGRAM_IMPORT_LOG:-}" ]; then
      echo "RUN" >> "$FAKE_ENGRAM_IMPORT_LOG"
      "${FAKE_PYTHON:-python3}" - "$PWD" "${ENGRAM_DATA_DIR:-$HOME/.engram}/engram.db" "$FAKE_ENGRAM_IMPORT_LOG" <<'PY'
import json, sqlite3, sys
from pathlib import Path
repo, db_path, log = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
db = sqlite3.connect(db_path)
done = {row[0] for row in db.execute("SELECT chunk_id FROM sync_chunks WHERE target_key = 'local'")}
with log.open("a") as out:
    for chunk in json.loads((repo / ".engram" / "manifest.json").read_text())["chunks"]:
        if chunk["id"] not in done:
            out.write("imported " + chunk["id"] + "\n")
            db.execute("INSERT INTO sync_chunks(target_key, chunk_id, imported_at) VALUES ('local', ?, datetime('now'))", (chunk["id"],))
db.commit()
PY
    fi
    # With FAKE_ENGRAM_IMPORT_OUTPUT set (a file name) it prints that text instead of the usual answer, as an Engram that
    # puts its own update notice before the result of the import.
    if [ -n "${FAKE_ENGRAM_IMPORT_OUTPUT:-}" ]; then cat "$FAKE_ENGRAM_IMPORT_OUTPUT"; exit 0; fi
    echo "No new chunks to import."; exit 0 ;;
  "sync --project")
    [ -n "${FAKE_ENGRAM_NOTHING:-}" ] && { echo "Nothing new to sync"; exit 0; }
    # a concurrent developer shares in the middle of this export
    if [ -n "${FAKE_ENGRAM_RACE:-}" ]; then FAKE_ENGRAM_RACE= bash "$FAKE_ENGRAM_RACE"; fi
    "${FAKE_PYTHON:-python3}" - "$PWD" "${FAKE_ENGRAM_TEXT:-text}" <<'PY'
import datetime, gzip, hashlib, json, sys
from pathlib import Path
repo, text = Path(sys.argv[1]), sys.argv[2]
data = {"sessions": [], "observations": [{"id": 1, "sync_id": "obs-" + hashlib.sha256(text.encode()).hexdigest()[:12], "type": "decision", "title": text, "content": text, "session_id": "s"}], "prompts": [{"id": 1, "content": "typed text"}], "mutations": []}
plain = json.dumps(data, separators=(",", ":"))
cid = hashlib.sha256(plain.encode()).hexdigest()[:8]
(repo / ".engram" / "chunks").mkdir(parents=True, exist_ok=True)
(repo / ".engram" / "chunks" / (cid + ".jsonl.gz")).write_bytes(gzip.compress(plain.encode()))
manifest_path = repo / ".engram" / "manifest.json"
manifest = json.loads(manifest_path.read_text())
manifest["chunks"].append({"id": cid, "created_by": "someone", "created_at": datetime.datetime.utcnow().isoformat() + "Z", "sessions": 0, "memories": 1, "prompts": 1})
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
PY
    # With FAKE_ENGRAM_LOCK_AFTER_EXPORT set (a file name) the database is write-locked for 3 s right after the export,
    # as when Engram is busy; the export waits until the lock is really taken.
    if [ -n "${FAKE_ENGRAM_LOCK_AFTER_EXPORT:-}" ]; then
      "${FAKE_PYTHON:-python3}" - "${ENGRAM_DATA_DIR:-$HOME/.engram}/engram.db" "$FAKE_ENGRAM_LOCK_AFTER_EXPORT" >/dev/null 2>&1 <<'PY' &
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("BEGIN EXCLUSIVE")
open(sys.argv[2], "w").close()
time.sleep(3)
PY
      for _ in $(seq 1 40); do [ -f "$FAKE_ENGRAM_LOCK_AFTER_EXPORT" ] && break; sleep 0.25; done
    fi
    echo "Created chunk"; exit 0 ;;
esac
exit 0
'''

# Stands in for GNU `timeout` / `gtimeout`: it logs "<limit><TAB><command>" and runs the command, unless the command
# contains FAKE_TIMEOUT_EXPIRE_ON: then the limit "expired" and it exits 124 without running anything.
FAKE_TIMEOUT = r'''#!/usr/bin/env bash
limit="$1"; shift
printf '%s\t%s\n' "$limit" "$*" >> "$FAKE_TIMEOUT_LOG"
case "$*" in *"${FAKE_TIMEOUT_EXPIRE_ON:-@@never@@}"*) exit 124 ;; esac
exec "$@"
'''

# Stands in for ssh: it never connects, it only logs how it was called.
FAKE_SSH = r'''#!/usr/bin/env bash
printf '@NAME@: %s\n' "$*" >> "$FAKE_SSH_LOG"
exit 255
'''

PATTERNS = {
    "personal_email_patterns": [r"dev1@example\.com"],
    "other_identity_patterns": [],
    "account": "devaccount",
    "person_name": "Dev One",
    "username": "devuser",
    "home_path": "/Users/devuser",
}


# Git for Windows ships core.autocrlf=true in its system configuration, and the script under test runs with
# GIT_CONFIG_NOSYSTEM=1: a clone made under that configuration would look modified to it (CRLF in the working copy).
# So the helper below reads the same configuration as the script. Other platforms keep the environment as it is.
HELPER_GIT_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull} if os.name == "nt" else None


def git(*args, cwd=None, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env or HELPER_GIT_ENV, capture_output=True, text=True, check=True).stdout


def signal_name(sig) -> str:
    return sig if isinstance(sig, str) else sig.name


windows_only = unittest.skipUnless(WINDOWS, "the Windows branch of the sync; macOS and Linux never enter it")


class SyncScriptTest(unittest.TestCase):
    def setUp(self):
        # A folder with spaces and accents, like "Mis Documentos" or "José": every path of the tools must be quoted.
        self._tmp = tempfile.TemporaryDirectory(prefix="memoria de José ")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.root = root
        self.home = root / "home"
        (self.home / ".config" / "engram-memory-tools").mkdir(parents=True)
        (self.home / ".config" / "engram-memory-tools" / "patterns.json").write_text(json.dumps(PATTERNS))
        (self.home / ".gitconfig").write_text("[user]\n\tname = Dev One\n\temail = dev1@example.com\n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.bin_dir = bin_dir
        write_script(bin_dir / "engram", FAKE_ENGRAM)
        self.env = {
            # nothing of the developer's real environment (git, Engram or fakes) may reach the script under test
            **{k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "ENGRAM_", "FAKE_"))},
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),  # Windows reads this one, not HOME: the cleaning step must not reach the real home
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "FAKE_PYTHON": sys.executable,  # what the fake engram runs, whatever `python3` means on this PATH
            "ENGRAM_MEMORY_LOCK_WAIT": "0",  # Windows only: a test of the wait sets its own
        }
        # the remote, and a seed commit with the REAL tools of this repository
        self.remote = root / "remote.git"
        git("init", "--bare", "-b", "main", str(self.remote))
        seed = root / "seed"
        seed.mkdir()
        git("init", "-b", "main", cwd=seed)
        shutil.copytree(REPO / "tools", seed / "tools", ignore=shutil.ignore_patterns("__pycache__"))
        (seed / ".engram").mkdir()
        (seed / ".engram" / "config.json").write_text('{"project_name":"fake"}')
        (seed / ".engram" / "manifest.json").write_text(json.dumps({"version": 2, "chunks": []}, indent=2) + "\n")
        shutil.copy(REPO / ".gitignore", seed / ".gitignore")
        git("add", "-A", cwd=seed)
        git("-c", "user.name=Seed", "-c", "user.email=seed@example.com", "commit", "-m", "seed", cwd=seed)
        git("remote", "add", "origin", str(self.remote), cwd=seed)
        git("push", "origin", "main", cwd=seed)

    def clone(self, name: str) -> Path:
        target = self.root / name
        git("clone", str(self.remote), str(target))
        git("config", "user.name", "Dev One", cwd=target)
        git("config", "user.email", "dev1@example.com", cwd=target)
        return target

    def sync(self, clone: Path, direction: str, **extra_env):
        env = {**self.env, **extra_env}
        return subprocess.run(["bash", str(clone / "tools" / "engram-memory-sync.sh"), direction], cwd=clone, env=env, capture_output=True, text=True)

    def remote_file(self, path: str) -> str:
        return git("--git-dir", str(self.remote), "show", f"main:{path}")

    def remote_bytes(self, path: str) -> bytes:
        return subprocess.run(["git", "--git-dir", str(self.remote), "show", f"main:{path}"], capture_output=True, check=True).stdout

    def remote_observation_texts(self) -> set:
        texts = set()
        for line in git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main").splitlines():
            if line.startswith(".engram/chunks/"):
                raw = subprocess.run(["git", "--git-dir", str(self.remote), "show", f"main:{line}"], capture_output=True, check=True).stdout
                texts.update(o["content"] for o in json.loads(gzip.decompress(raw))["observations"])
        return texts

    # --- helpers for the Engram database and for the time limits ------------------------------------------------

    def engram_machine(self, name: str, table: bool = True) -> dict:
        """The Engram of one developer: its own database (with the sync_chunks table, unless table=False) and the log
        where the fake `engram sync --import` writes what it did. Returns the environment variables that select them."""
        data = self.root / f"engram-{name}"
        data.mkdir()
        db = sqlite3.connect(data / "engram.db")
        if table:
            db.execute("CREATE TABLE sync_chunks (target_key TEXT NOT NULL, chunk_id TEXT NOT NULL, imported_at TEXT NOT NULL, PRIMARY KEY (target_key, chunk_id))")
        else:
            db.execute("CREATE TABLE sessions (id TEXT)")
        db.commit()
        db.close()
        return {"ENGRAM_DATA_DIR": str(data), "FAKE_ENGRAM_IMPORT_LOG": str(self.root / f"imports-{name}.log")}

    def marked_imported(self, machine: dict) -> set:
        db = sqlite3.connect(Path(machine["ENGRAM_DATA_DIR"]) / "engram.db")
        try:
            return {row[0] for row in db.execute("SELECT chunk_id FROM sync_chunks WHERE target_key = 'local'")}
        finally:
            db.close()

    def forget_marks(self, machine: dict):
        """What an interrupted run leaves behind: the marks that it should have written are missing."""
        db = sqlite3.connect(Path(machine["ENGRAM_DATA_DIR"]) / "engram.db")
        db.execute("DELETE FROM sync_chunks")
        db.commit()
        db.close()

    def reset_import_log(self, machine: dict):
        Path(machine["FAKE_ENGRAM_IMPORT_LOG"]).unlink(missing_ok=True)

    def import_runs(self, machine: dict):
        """(how many times `engram sync --import` ran, the ids that it imported)."""
        log = Path(machine["FAKE_ENGRAM_IMPORT_LOG"])
        lines = log.read_text().splitlines() if log.exists() else []
        return lines.count("RUN"), [line.split()[1] for line in lines if line.startswith("imported ")]

    def manifest_ids(self, clone: Path) -> list:
        return [c["id"] for c in json.loads((clone / ".engram" / "manifest.json").read_text())["chunks"]]

    def without_timeout_commands(self, install=None):
        """PATH with every command of the real PATH except `timeout` and `gtimeout` (the machine that runs these tests
        may have either of them); `install` adds the fake timeout under that name."""
        farm = self.root / "path-without-timeout"
        farm.mkdir(exist_ok=True)
        for fake in ("timeout", "gtimeout"):
            (self.bin_dir / fake).unlink(missing_ok=True)
        fill_windows_farm(farm, lambda name: name in ("timeout", "gtimeout"))
        for directory in [] if WINDOWS else os.environ["PATH"].split(os.pathsep):
            if not os.path.isdir(directory):
                continue
            for entry in os.scandir(directory):
                if entry.name in ("timeout", "gtimeout") or os.path.lexists(farm / entry.name):
                    continue
                try:
                    runnable = entry.is_file() and os.access(entry.path, os.X_OK)
                except OSError:  # an entry that this user is not even allowed to look at
                    continue
                if runnable:
                    os.symlink(entry.path, farm / entry.name)
        self.env["PATH"] = f"{self.bin_dir}{os.pathsep}{farm}"
        if install:
            write_script(self.bin_dir / install, FAKE_TIMEOUT)

    def timeout_calls(self, log: Path) -> list:
        """[(limit, command), ...] of the network calls and the `engram sync` calls the script ran through the fake
        timeout. The probes that look for a working Python go through it too, with a limit of their own."""
        return [call for call in self.all_timeout_calls(log) if "sys.version_info" not in call[1]]

    def all_timeout_calls(self, log: Path) -> list:
        return [tuple(line.split("\t", 1)) for line in log.read_text().splitlines()] if log.exists() else []

    # --- finding a working Python: on Windows `python3` is often only the shortcut to the Microsoft Store -------------

    def windows_store_python3(self):
        """A `python3` that does what the Microsoft Store shortcut does: print a message and run nothing."""
        stub = self.bin_dir / "python3"
        stub.write_text("#!/bin/sh\necho 'Python was not found; run without arguments to install from the Microsoft Store, or disable this shortcut from Settings > Manage App Execution Aliases.' >&2\nexit 49\n")
        stub.chmod(0o755)

    def link_python_as(self, name: str, directory: Path = None) -> Path:
        directory = directory or self.bin_dir
        directory.mkdir(parents=True, exist_ok=True)
        link_command(sys.executable, directory / name)
        return directory / name

    def without_python_commands(self):
        """PATH with every command of the real PATH except the ones that are Python (python, python3, python3.12, py...)."""
        farm = self.root / "path-without-python"
        farm.mkdir(exist_ok=True)
        python_name = re.compile(r"(py|pythonw?|python[0-9.]+[a-z]*)(\.exe)?")
        fill_windows_farm(farm, python_name.fullmatch)
        for directory in [] if WINDOWS else os.environ["PATH"].split(os.pathsep):
            if not os.path.isdir(directory):
                continue
            for entry in os.scandir(directory):
                if python_name.fullmatch(entry.name) or os.path.lexists(farm / entry.name):
                    continue
                try:
                    runnable = entry.is_file() and os.access(entry.path, os.X_OK)
                except OSError:
                    continue
                if runnable:
                    os.symlink(entry.path, farm / entry.name)
        self.env["PATH"] = f"{self.bin_dir}{os.pathsep}{farm}"
        for name in ("python", "python3", "py"):
            self.assertIsNone(shutil.which(name, path=str(farm)), f"the host leaks {name} into the test's PATH")

    def install_timeout_that_really_ends_commands(self):
        """A `timeout` that does what GNU timeout does (macOS has none): end the command after N seconds, status 124."""
        shim = self.bin_dir / "timeout"
        if WINDOWS:  # the real one is there, and this Python cannot make process groups
            write_script(shim, '#!/bin/sh\nexec /usr/bin/timeout "$@"\n')
            return
        shim.write_text(
            '#!/bin/sh\nexec "$FAKE_PYTHON" -c \'\nimport os, signal, subprocess, sys\n'
            "limit, command = float(sys.argv[1]), sys.argv[2:]\n"
            "process = subprocess.Popen(command, start_new_session=True)\n"
            "def forward(number, frame):\n    os.killpg(process.pid, signal.SIGTERM)\n    sys.exit(143)\n"
            "signal.signal(signal.SIGTERM, forward)\n"
            "try:\n    sys.exit(process.wait(timeout=limit))\n"
            "except subprocess.TimeoutExpired:\n    os.killpg(process.pid, signal.SIGKILL)\n    process.wait()\n    sys.exit(124)\n"
            '\' "$@"\n'
        )
        shim.chmod(0o755)

    def test_a_python_that_hangs_does_not_stop_the_sync(self):
        clone = self.clone("dev-a")
        hanging = self.bin_dir / "python3"
        hanging.write_text("#!/bin/sh\nexec sleep 60\n")
        hanging.chmod(0o755)
        self.link_python_as("python")
        self.install_timeout_that_really_ends_commands()

        started = time.monotonic()
        result = self.sync(clone, "down", ENGRAM_MEMORY_PROBE_SECONDS="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("No new chunks to import", result.stderr)

    def test_a_missing_helper_is_reported_by_name_and_nothing_is_done(self):
        clone = self.clone("dev-a")
        (clone / "tools" / "find-python.sh").unlink()

        result = self.sync(clone, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("find-python.sh", result.stderr)
        self.assertNotIn("command not found", result.stderr)
        self.assertNotIn("No new chunks to import", result.stderr)

    def test_the_message_says_what_each_python_answered(self):
        clone = self.clone("dev-a")
        stub = self.bin_dir / "python3"
        stub.write_text("#!/bin/sh\necho 'STUB-SAID-THIS first line' >&2\necho 'second line' >&2\nexit 49\n")
        stub.chmod(0o755)
        self.without_python_commands()

        result = self.sync(clone, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("python3: STUB-SAID-THIS first line", result.stderr)
        self.assertNotIn("second line", result.stderr)

    def test_the_chosen_python_may_be_a_command_with_arguments(self):
        clone = self.clone("dev-a")
        self.windows_store_python3()
        self.without_python_commands()
        launcher = self.bin_dir / "py"
        launcher.write_text('#!/bin/sh\n[ "$1" = "-3" ] || exit 1\nshift\nexec "$FAKE_PYTHON" "$@"\n')
        launcher.chmod(0o755)

        result = self.sync(clone, "down", ENGRAM_MEMORY_PYTHON="py -3")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No new chunks to import", result.stderr)

    def test_sharing_works_through_the_py_launcher_in_every_call_that_uses_python(self):
        a, machine = self.clone("dev-a"), self.engram_machine("a")
        self.windows_store_python3()
        self.without_python_commands()
        launcher = self.bin_dir / "py"
        launcher.write_text('#!/bin/sh\n[ "$1" = "-3" ] || exit 1\nshift\nexec "$FAKE_PYTHON" "$@"\n')
        launcher.chmod(0o755)

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge shared through py", **machine)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_observation_texts(), {"knowledge shared through py"})
        (chunk,) = self.manifest_ids(a)
        self.assertEqual(self.marked_imported(machine), {chunk})

    def test_a_python3_that_is_only_the_store_shortcut_is_skipped_for_the_python_that_works(self):
        clone = self.clone("dev-a")
        self.windows_store_python3()
        self.link_python_as("python")

        result = self.sync(clone, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("cannot read the project name", result.stderr)
        self.assertIn("No new chunks to import", result.stderr)

    def test_the_py_launcher_is_the_last_resort(self):
        clone = self.clone("dev-a")
        self.windows_store_python3()
        self.without_python_commands()
        launcher = self.bin_dir / "py"
        launcher.write_text('#!/bin/sh\n[ "$1" = "-3" ] || exit 1\nshift\nexec "$FAKE_PYTHON" "$@"\n')
        launcher.chmod(0o755)

        result = self.sync(clone, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No new chunks to import", result.stderr)

    def test_with_no_working_python_it_says_so_by_name_and_shares_nothing(self):
        clone = self.clone("dev-a")
        self.windows_store_python3()
        self.without_python_commands()

        result = self.sync(clone, "up", FAKE_ENGRAM_TEXT="knowledge that must not be shared")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no working Python", result.stderr)
        self.assertIn("python3, python and py -3", result.stderr)
        self.assertIn("Microsoft Store", result.stderr)
        self.assertIn("ENGRAM_MEMORY_PYTHON", result.stderr)
        self.assertNotIn("project name", result.stderr)
        self.assertEqual(self.remote_observation_texts(), set())

    def test_the_python_can_be_chosen_with_a_path_that_holds_spaces(self):
        clone = self.clone("dev-a")
        self.windows_store_python3()
        self.without_python_commands()
        chosen = self.link_python_as("python", self.root / "My Python 3.12")

        result = self.sync(clone, "down", ENGRAM_MEMORY_PYTHON=str(chosen))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No new chunks to import", result.stderr)

    def test_a_chosen_python_that_does_not_work_is_reported_and_not_replaced_silently(self):
        clone = self.clone("dev-a")  # a working python3 IS on the PATH: the wrong choice must still be an error

        result = self.sync(clone, "down", ENGRAM_MEMORY_PYTHON=str(self.root / "no-such-python"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ENGRAM_MEMORY_PYTHON", result.stderr)
        self.assertIn("no working Python", result.stderr)
        self.assertNotIn("No new chunks to import", result.stderr)

    def test_sharing_works_with_the_python_that_is_not_called_python3(self):
        clone = self.clone("dev-a")
        self.windows_store_python3()
        self.link_python_as("python")

        result = self.sync(clone, "up", FAKE_ENGRAM_TEXT="knowledge from a Windows machine")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_observation_texts(), {"knowledge from a Windows machine"})
        self.assertNotIn("Traceback", result.stderr)

    def test_the_chunk_a_machine_shares_is_recorded_as_imported_also_without_python3(self):
        a, machine = self.clone("dev-a"), self.engram_machine("a")
        self.windows_store_python3()
        self.link_python_as("python")

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A", **machine)

        self.assertEqual(result.returncode, 0, result.stderr)
        (chunk,) = self.manifest_ids(a)
        self.assertEqual(self.marked_imported(machine), {chunk})

    def test_two_developers_sharing_at_the_same_time_lose_nothing_also_without_python3(self):
        a, b = self.clone("dev-a"), self.clone("dev-b")
        self.windows_store_python3()
        self.link_python_as("python")
        race = self.root / "race.sh"
        race.write_text(f'cd "{a}" && FAKE_ENGRAM_TEXT="knowledge from A" bash tools/engram-memory-sync.sh up\n')

        result = self.sync(b, "up", FAKE_ENGRAM_TEXT="knowledge from B", FAKE_ENGRAM_RACE=str(race))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_observation_texts(), {"knowledge from A", "knowledge from B"})
        self.assertEqual(len(json.loads(self.remote_file(".engram/manifest.json"))["chunks"]), 2)

    # --- a dry run, the lock and the project name -----------------------------------------------------------------

    def test_a_dry_run_leaves_what_the_developer_had_staged_outside_the_memory_staged(self):
        a = self.clone("dev-a")
        (a / "notes.txt").write_text("my own work, staged on purpose\n")
        git("add", "notes.txt", cwd=a)

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge", ENGRAM_MEMORY_DRY_RUN="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run", result.stderr)
        self.assertEqual(git("diff", "--cached", "--name-only", cwd=a).split(), ["notes.txt"])  # nothing of .engram stays staged

    def make_lock(self, clone: Path, pid: int, age_seconds: int = 0):
        lock = clone / ".engram" / ".sync.lock"
        lock.mkdir()
        (lock / "pid").write_text(f"{pid}\n")
        if age_seconds:
            old = time.time() - age_seconds
            os.utime(lock / "pid", (old, old))
            os.utime(lock, (old, old))
        return lock

    def test_a_fresh_lock_of_a_process_that_is_alive_makes_this_run_wait_its_turn(self):
        a = self.clone("dev-a")
        self.make_lock(a, start_live_process(self))

        result = self.sync(a, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("another sync is running", result.stderr)
        self.assertNotIn("No new chunks to import", result.stderr)

    def test_an_old_lock_is_taken_over_even_when_its_process_number_belongs_to_someone_else_now(self):
        # A recycled process number looks alive forever: a sync takes seconds, so an hour-old lock is a dead one.
        a = self.clone("dev-a")
        self.make_lock(a, start_live_process(self), age_seconds=2 * 3600)

        result = self.sync(a, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("another sync is running", result.stderr)
        self.assertIn("No new chunks to import", result.stderr)

    def test_a_fresh_lock_with_no_pid_yet_is_one_being_taken_not_a_dead_one(self):
        # The owner makes the folder and then writes its pid: in between, the lock must be respected.
        a = self.clone("dev-a")
        lock = a / ".engram" / ".sync.lock"
        lock.mkdir()

        result = self.sync(a, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("another sync is starting", result.stderr)
        self.assertNotIn("No new chunks to import", result.stderr)

    def test_an_old_lock_with_no_pid_is_taken_over(self):
        a = self.clone("dev-a")
        lock = a / ".engram" / ".sync.lock"
        lock.mkdir()
        old = time.time() - 300
        os.utime(lock, (old, old))

        result = self.sync(a, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("another sync", result.stderr)
        self.assertIn("No new chunks to import", result.stderr)

    def test_the_raw_originals_of_a_run_stay_in_the_home_of_the_test(self):
        a = self.clone("dev-a")

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(list((self.home / ".engram" / "local-only").rglob("*.jsonl.gz")), result.stderr)

    # --- Windows: a session that resumes fires two hooks seconds apart, and the first one may lose its reader -----------

    @windows_only
    def test_a_sync_whose_reader_is_gone_still_imports(self):
        a, machine = self.clone("dev-a"), self.engram_machine("a")
        (a / ".engram" / "left-by-a-previous-run.txt").write_text("not shared yet")  # so it has a line to say before the import
        reading, writing = os.pipe()
        process = subprocess.Popen(["bash", str(a / "tools" / "engram-memory-sync.sh"), "down"], cwd=a, env={**self.env, **machine}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=writing)
        os.close(writing)
        os.close(reading)  # what is left of the hook that started it: nobody reads what it says

        self.assertEqual(process.wait(timeout=60), 0)
        self.assertEqual(self.import_runs(machine)[0], 1, "it died at the line it could not write, before the import")

    def start_sync_that_holds_the_lock(self, clone: Path, **extra_env):
        process = subprocess.Popen(["bash", str(clone / "tools" / "engram-memory-sync.sh"), "down"], cwd=clone, env={**self.env, **extra_env}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(process.wait)
        pid_file = clone / ".engram" / ".sync.lock" / "pid"
        for _ in range(400):
            if pid_file.exists() and pid_file.read_text().strip():
                return process
            time.sleep(0.05)
        self.fail("the first sync never took the lock")

    @windows_only
    def test_a_down_waits_for_the_sync_that_holds_the_lock_and_imports_after_it(self):
        a, machine = self.clone("dev-a"), self.engram_machine("a")
        self.start_sync_that_holds_the_lock(a, FAKE_ENGRAM_SLOW="4", **machine)

        result = self.sync(a, "down", ENGRAM_MEMORY_LOCK_WAIT="40", **machine)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("another sync", result.stderr)
        self.assertIn("No new chunks to import", result.stderr)
        self.assertEqual(self.import_runs(machine)[0], 2)

    @windows_only
    def test_the_wait_for_the_lock_has_a_limit_and_then_the_run_steps_aside(self):
        a = self.clone("dev-a")
        self.make_lock(a, start_live_process(self))

        started = time.monotonic()
        result = self.sync(a, "down", ENGRAM_MEMORY_LOCK_WAIT="2")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 2)
        self.assertLess(time.monotonic() - started, 20)  # far from the 60 seconds of the hook of Claude Code
        self.assertIn("another sync is running", result.stderr)
        self.assertNotIn("No new chunks to import", result.stderr)

    @windows_only
    def test_an_up_does_not_wait_for_the_lock(self):
        a = self.clone("dev-a")
        self.make_lock(a, start_live_process(self))

        started = time.monotonic()
        result = self.sync(a, "up", ENGRAM_MEMORY_LOCK_WAIT="30")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 20)
        self.assertIn("another sync is running", result.stderr)

    @windows_only
    def test_the_programs_of_system32_with_the_same_names_are_not_used(self):
        # Git for Windows put at the end of the PATH: its usr/bin comes after System32, whose `timeout`, `find` and
        # `sort` are other programs (`timeout 90 git pull` only prints a syntax error).
        a, b, machine = self.clone("dev-a"), self.clone("dev-b"), self.engram_machine("b")
        self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A")
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        self.env["PATH"] = f"{self.bin_dir}{os.pathsep}{system32}{os.pathsep}{os.environ['PATH']}"

        result = self.sync(b, "down", **machine)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("failed", result.stderr)
        self.assertEqual(self.import_runs(machine)[0], 1, result.stderr)
        self.assertEqual(self.manifest_ids(a), self.manifest_ids(b))

    @unittest.skipIf(WINDOWS, "on Windows a down waits for the lock on purpose")
    def test_on_macos_and_linux_a_lock_in_use_is_never_waited_for(self):
        a = self.clone("dev-a")
        self.make_lock(a, start_live_process(self))

        started = time.monotonic()
        result = self.sync(a, "down", ENGRAM_MEMORY_LOCK_WAIT="30")

        self.assertLess(time.monotonic() - started, 20)
        self.assertIn("another sync is running", result.stderr)

    def test_every_call_to_engram_has_its_own_update_check_switched_off(self):
        a = self.clone("dev-a")
        log = self.root / "engram-env.log"

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A", FAKE_ENGRAM_ENV_LOG=str(log), ENGRAM_NO_UPDATE_CHECK="")

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text().splitlines()
        self.assertIn("sync --import 1", calls)
        self.assertIn("sync --project 1", calls)
        self.assertEqual([c for c in calls if not c.endswith(" 1")], [])

    def test_a_real_run_commits_only_the_memory_and_leaves_other_staged_work_staged(self):
        a = self.clone("dev-a")
        (a / "notes.txt").write_text("my own work, staged on purpose\n")
        git("add", "notes.txt", cwd=a)

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shared", result.stderr)
        self.assertEqual(git("show", "--name-only", "--format=", "HEAD", cwd=a).split().count("notes.txt"), 0)
        self.assertEqual(git("diff", "--cached", "--name-only", cwd=a).split(), ["notes.txt"])
        self.assertEqual(self.remote_observation_texts(), {"knowledge from A"})

    def test_a_dry_run_with_only_other_staged_work_has_nothing_of_the_memory_to_report(self):
        a = self.clone("dev-a")
        (a / "notes.txt").write_text("my own work, staged on purpose\n")
        git("add", "notes.txt", cwd=a)

        result = self.sync(a, "up", FAKE_ENGRAM_NOTHING="1", ENGRAM_MEMORY_DRY_RUN="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("a new cleaned chunk is ready", result.stderr)

    def test_a_project_name_with_letters_outside_the_machine_code_page_is_read(self):
        try:
            installed = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout.split()
        except OSError:  # no `locale` program on this machine
            installed = []
        locale = next((name for name in installed if name.lower() in ("en_us.iso88591", "en_us.iso8859-1")), None)
        if locale is None:
            self.skipTest("no ISO-8859-1 locale on this machine to stand for a code page that is not UTF-8")
        a = self.clone("dev-a")
        (a / ".engram" / "config.json").write_text(json.dumps({"project_name": "Łódź"}), encoding="utf-8")

        result = self.sync(a, "down", LC_ALL=locale)  # the spelling this machine has, not another

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("cannot read the project name", result.stderr)
        self.assertIn("No new chunks to import", result.stderr)

    # --- the sync is ended from outside (the hook of Claude Code gives up after 60 seconds) -------------------------------

    def start_hanging_sync(self, clone: Path, direction: str = "down", pid_file=None, script: str = "engram-memory-sync.sh", **extra_env):
        # one pid file per sync: a file left by a previous one would make the wait below end before the program hangs
        hung = pid_file or self.root / f"hung-engram-{clone.name}.pid"
        process = subprocess.Popen(
            ["bash", str(clone / "tools" / script), direction],
            cwd=clone, env={**self.env, "FAKE_ENGRAM_HANG": str(hung), **extra_env}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        for _ in range(200):  # until the fake engram is hanging
            if hung.exists() and hung.read_text().strip():
                break
            time.sleep(0.05)
        else:
            process.kill()
            self.fail("the sync never reached the import")
        self.addCleanup(process.stderr.close)
        return process, int(hung.read_text().strip())

    def alive(self, pid: int) -> bool:
        if WINDOWS:
            return shell_kill("0", pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def end_and_check(self, process, hung_pid: int, clone: Path, sig):
        self.addCleanup(lambda: self.alive(hung_pid) and (shell_kill("9", hung_pid) if WINDOWS else os.kill(hung_pid, 9)))
        if WINDOWS:
            # send_signal is TerminateProcess there, which no script can react to: the shell's own kill reaches the trap.
            # That is what a shell does; what Node does to a hook is the known limit in the README.
            self.assertTrue(shell_kill(signal_name(sig), int((clone / ".engram" / ".sync.lock" / "pid").read_text().strip())))
        else:
            process.send_signal(sig)  # what a hook that gives up does
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            self.fail(f"the sync did not react to {signal_name(sig)}")
        for _ in range(100):
            if not self.alive(hung_pid):
                break
            time.sleep(0.05)
        self.assertFalse(self.alive(hung_pid), "the program it was waiting for was left running")
        self.assertFalse((clone / ".engram" / ".sync.lock").exists(), "the lock was left behind")
        self.assertEqual(process.returncode, 143, "it must leave like a process that was ended by a signal")

    def test_when_the_sync_is_asked_to_end_it_ends_the_engram_it_waits_for_and_leaves_no_lock(self):
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", "HUP")):  # the shell of Windows has HUP, its Python does not
            with self.subTest(signal=signal_name(sig)):
                clone = self.clone(f"dev-{signal_name(sig).lower()}")
                process, engram_pid = self.start_hanging_sync(clone)

                self.end_and_check(process, engram_pid, clone, sig)

    def test_the_same_holds_when_a_timeout_program_stands_between_the_sync_and_engram(self):
        clone = self.clone("dev-a")
        self.install_timeout_that_really_ends_commands()
        process, engram_pid = self.start_hanging_sync(clone)

        self.end_and_check(process, engram_pid, clone, signal.SIGTERM)

    def test_an_engram_that_ignores_the_request_to_end_is_forced(self):
        clone = self.clone("dev-a")
        process, engram_pid = self.start_hanging_sync(clone, FAKE_ENGRAM_DEAF="1")

        self.end_and_check(process, engram_pid, clone, signal.SIGTERM)

    def test_the_same_holds_when_it_is_a_git_pull_that_hangs(self):
        clone = self.clone("dev-a")
        real_git = shutil.which("git")
        hung = self.root / "hung-git.pid"
        wrapper = self.bin_dir / "git"
        wrapper.write_text(f'#!/bin/sh\nfor a in "$@"; do\n  if [ "$a" = pull ]; then echo "$$" > "{hung}"; exec sleep 300; fi\ndone\nexec "{real_git}" "$@"\n')
        wrapper.chmod(0o755)
        process = subprocess.Popen(["bash", str(clone / "tools" / "engram-memory-sync.sh"), "down"], cwd=clone, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(200):
            if hung.exists() and hung.read_text().strip():
                break
            time.sleep(0.05)
        else:
            process.kill()
            self.fail("the sync never reached the pull")

        self.end_and_check(process, int(hung.read_text().strip()), clone, signal.SIGTERM)

    def install_timeout_that_waits_for_its_command(self):
        """A `timeout` like GNU's when its command ignores the request to end: it passes the signal on and keeps
        waiting, so killing only the timeout program would leave the command running."""
        shim = self.bin_dir / "timeout"
        if WINDOWS:  # a Python here cannot catch a signal of the shell
            write_script(shim, """#!/usr/bin/env bash
shift
"$@" &
child=$!
trap 'kill -TERM "$child"' TERM
while kill -0 "$child" 2>/dev/null; do wait "$child"; done
""")
            return
        shim.write_text(
            '#!/bin/sh\nexec "$FAKE_PYTHON" -c \'\nimport signal, subprocess, sys\n'
            "process = subprocess.Popen(sys.argv[2:])\n"
            "signal.signal(signal.SIGTERM, lambda number, frame: process.send_signal(signal.SIGTERM))\n"
            "while True:\n    try:\n        sys.exit(process.wait())\n    except InterruptedError:\n        pass\n"
            '\' "$@"\n'
        )
        shim.chmod(0o755)

    def test_an_engram_that_ignores_the_request_behind_a_timeout_program_is_forced_too(self):
        clone = self.clone("dev-a")
        self.install_timeout_that_waits_for_its_command()
        process, engram_pid = self.start_hanging_sync(clone, FAKE_ENGRAM_DEAF="1")

        self.end_and_check(process, engram_pid, clone, signal.SIGTERM)

    def test_the_cleaning_step_is_ended_too_when_the_sync_is_asked_to_end(self):
        clone = self.clone("dev-a")
        hung = self.root / "hung-cleaning.pid"
        shim = self.bin_dir / "python3"
        shim.write_text(f'#!/bin/sh\ncase "$*" in *sanitize_chunks.py*) echo "$$" > "{hung}"; exec sleep 300 ;; esac\nexec "$FAKE_PYTHON" "$@"\n')
        shim.chmod(0o755)
        process, cleaning_pid = self.start_hanging_sync(clone, "up", hung, FAKE_ENGRAM_HANG="")

        self.end_and_check(process, cleaning_pid, clone, signal.SIGTERM)

    def test_the_record_of_imported_chunks_is_ended_too_when_the_sync_is_asked_to_end(self):
        a, b, machine = self.clone("dev-a"), self.clone("dev-b"), self.engram_machine("b")
        self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A")
        # B has a chunk of its own that nobody has published: the next down records it before it imports
        self.sync(b, "up", FAKE_ENGRAM_TEXT="knowledge from B", ENGRAM_MEMORY_DRY_RUN="1", **machine)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sqlite3, sys, time\ndb = sqlite3.connect(sys.argv[1], isolation_level=None)\ndb.execute('BEGIN EXCLUSIVE')\nprint('locked', flush=True)\ntime.sleep(60)", str(Path(machine["ENGRAM_DATA_DIR"]) / "engram.db")],
            stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(holder.kill)
        self.addCleanup(holder.stdout.close)
        holder.stdout.readline()
        hung = self.root / "hung-record.pid"
        shim = self.bin_dir / "python3"
        shim.write_text(f'#!/bin/sh\ncase " $* " in *" -I - "*) echo "$$" > "{hung}" ;; esac\nexec "$FAKE_PYTHON" "$@"\n')
        shim.chmod(0o755)
        process, recording_pid = self.start_hanging_sync(b, "down", hung, FAKE_ENGRAM_HANG="", ENGRAM_MEMORY_DB_TIMEOUT="60", **machine)

        self.end_and_check(process, recording_pid, b, signal.SIGTERM)

    def test_the_cleaning_step_is_not_cut_by_the_time_limit_of_the_network_calls(self):
        clone = self.clone("dev-a")
        self.install_timeout_that_really_ends_commands()
        slow = self.bin_dir / "python3"
        slow.write_text('#!/bin/sh\ncase "$*" in *sanitize_chunks.py*) sleep 3.5 ;; esac\nexec "$FAKE_PYTHON" "$@"\n')
        slow.chmod(0o755)

        result = self.sync(clone, "up", FAKE_ENGRAM_TEXT="knowledge from A", ENGRAM_MEMORY_TIMEOUT="2")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("STOP", result.stderr)
        self.assertEqual(self.remote_observation_texts(), {"knowledge from A"})

    def test_the_import_does_not_need_mktemp_and_leaves_nothing_in_the_temporary_folder(self):
        a, b, machine = self.clone("dev-a"), self.clone("dev-b"), self.engram_machine("b")
        self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A")
        broken = self.bin_dir / "mktemp"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(0o755)
        scratch = self.root / "scratch-tmp"
        scratch.mkdir()

        result = self.sync(b, "down", TMPDIR=str(scratch), **machine)

        self.assertEqual(result.returncode, 0, result.stderr)
        (chunk,) = self.manifest_ids(b)
        self.assertEqual(self.import_runs(machine), (1, [chunk]), result.stderr)
        self.assertEqual(sorted(p.name for p in scratch.iterdir()), [])

    def test_two_developers_sharing_at_the_same_time_lose_nothing(self):
        a, b = self.clone("dev-a"), self.clone("dev-b")
        # While B is exporting, A shares its own memory: B's push will be rejected.
        race = self.root / "race.sh"
        race.write_text(f'cd "{a}" && FAKE_ENGRAM_TEXT="knowledge from A" bash tools/engram-memory-sync.sh up\n')

        result = self.sync(b, "up", FAKE_ENGRAM_TEXT="knowledge from B", FAKE_ENGRAM_RACE=str(race))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shared", result.stderr)
        self.assertEqual(self.remote_observation_texts(), {"knowledge from A", "knowledge from B"})
        self.assertEqual(len(json.loads(self.remote_file(".engram/manifest.json"))["chunks"]), 2)
        self.assertEqual(git("status", "--porcelain", cwd=b), "")
        self.assertFalse((b / ".git" / "rebase-merge").exists())

    def test_what_a_developer_shares_reaches_the_others_on_their_next_down(self):
        a, b = self.clone("dev-a"), self.clone("dev-b")
        self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A")

        result = self.sync(b, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads((b / ".engram" / "manifest.json").read_text())["chunks"]), 1)

    def test_engram_runs_in_the_memory_repository_whatever_folder_the_hook_was_started_from(self):
        # A hook starts in the root of the PROJECT, not in the memory repository: Engram reads .engram from where it runs.
        a, b, machine = self.clone("dev-a"), self.clone("dev-b"), self.engram_machine("b")
        self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A")
        elsewhere = self.root / "a-project-folder"
        elsewhere.mkdir()

        result = subprocess.run(["bash", str(b / "tools" / "engram-memory-sync.sh"), "down"], cwd=elsewhere, env={**self.env, **machine}, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        (chunk,) = self.manifest_ids(b)
        self.assertEqual(self.import_runs(machine), (1, [chunk]))

    def test_engram_runs_in_the_memory_repository_also_when_a_timeout_program_runs_it(self):
        a, b, machine = self.clone("dev-a"), self.clone("dev-b"), self.engram_machine("b")
        self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A")
        self.install_timeout_that_really_ends_commands()
        elsewhere = self.root / "a-project-folder"
        elsewhere.mkdir()

        result = subprocess.run(["bash", str(b / "tools" / "engram-memory-sync.sh"), "down"], cwd=elsewhere, env={**self.env, **machine}, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        (chunk,) = self.manifest_ids(b)
        self.assertEqual(self.import_runs(machine), (1, [chunk]))

    def test_a_developer_without_the_privacy_file_gets_it_made_and_shares_cleaned_memory(self):
        a = self.clone("dev-a")
        (self.home / ".config" / "engram-memory-tools" / "patterns.json").unlink()

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="Ask dev1@example.com, said Dev One")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".config" / "engram-memory-tools" / "patterns.json").exists())
        shared = self.remote_observation_texts()
        self.assertEqual(len(shared), 1)
        text = shared.pop()
        self.assertNotIn("dev1@example.com", text)
        self.assertNotIn("Dev One", text)

    def test_the_pull_itself_makes_the_privacy_file(self):
        # Configuration is done when the memory comes in, not by hand and not on the first push.
        a = self.clone("dev-a")
        (self.home / ".config" / "engram-memory-tools" / "patterns.json").unlink()

        result = self.sync(a, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".config" / "engram-memory-tools" / "patterns.json").exists())

    def test_the_privacy_file_is_made_with_the_tools_that_the_pull_just_brought(self):
        # A clone older than the fix must not build the file with its old setup script.
        stale = self.clone("stale")
        fresh = self.clone("fresh")
        (fresh / "tools" / "setup.sh").write_text(
            "#!/usr/bin/env bash\nmkdir -p \"$HOME/.config/engram-memory-tools\"\n"
            "echo '{\"personal_email_patterns\": [], \"other_identity_patterns\": [], \"account\": \"from-new-tools\", "
            "\"person_name\": \"from-new-tools\", \"username\": \"from-new-tools\", \"home_path\": \"/nowhere\"}' "
            "> \"$HOME/.config/engram-memory-tools/patterns.json\"\n"
        )
        git("add", "-A", cwd=fresh)
        git("commit", "-m", "newer setup", cwd=fresh)
        git("push", "origin", "main", cwd=fresh)
        (self.home / ".config" / "engram-memory-tools" / "patterns.json").unlink()

        result = self.sync(stale, "up", FAKE_ENGRAM_TEXT="knowledge")

        self.assertEqual(result.returncode, 0, result.stderr)
        made = json.loads((self.home / ".config" / "engram-memory-tools" / "patterns.json").read_text())
        self.assertEqual(made["account"], "from-new-tools")

    def publish_newer_sync_script(self, probe: str = ""):
        """A second developer pushes a version of the sync script that says so as soon as it configures a machine
        (and, with `probe`, also runs that extra shell line there)."""
        fresh = self.clone("fresh")
        script = fresh / "tools" / "engram-memory-sync.sh"
        text = script.read_text()
        old = "ensure_git_identity() {\n"
        assert text.count(old) == 1
        script.write_text(text.replace(old, old + '  say "NEW SYNC LOGIC"\n' + probe))
        git("add", "-A", cwd=fresh)
        git("commit", "-m", "newer sync script", cwd=fresh)
        git("push", "origin", "main", cwd=fresh)

    def test_a_pull_that_replaces_the_sync_script_runs_the_new_one_right_away_on_down(self):
        # The copy that is running is the old one: without a restart it would go on with the old logic, and the
        # configuration added by the new version would only appear on the NEXT pull.
        stale = self.clone("stale")
        self.publish_newer_sync_script()

        result = self.sync(stale, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NEW SYNC LOGIC", result.stderr)
        self.assertFalse((stale / ".engram" / ".sync.lock").exists())

    def test_a_pull_that_replaces_the_sync_script_runs_the_new_one_right_away_on_up(self):
        stale = self.clone("stale")
        self.publish_newer_sync_script()

        result = self.sync(stale, "up", FAKE_ENGRAM_TEXT="knowledge shared by a stale clone")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NEW SYNC LOGIC", result.stderr)
        self.assertIn("shared", result.stderr)
        self.assertIn("knowledge shared by a stale clone", self.remote_observation_texts())

    def test_the_restart_happens_only_once(self):
        stale = self.clone("stale")
        self.publish_newer_sync_script()

        # a process that is already the restarted one must never restart again, whatever it finds on disk
        result = self.sync(stale, "down", ENGRAM_MEMORY_REEXEC="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("NEW SYNC LOGIC", result.stderr)

    def test_a_script_that_did_not_change_is_not_restarted(self):
        a = self.clone("dev-a")

        result = self.sync(a, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("memory of fake"), 1)

    def test_the_import_line_names_the_project_and_repeats_what_engram_said(self):
        a = self.clone("dev-a")

        result = self.sync(a, "down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.splitlines().count("[engram-memory] memory of fake: No new chunks to import."), 1, result.stderr)

    def import_summary(self, engram_says: str) -> list:
        """The `memory of fake: ...` lines that the sync printed when `engram sync --import` answered `engram_says`."""
        output = self.root / "engram-import-output.txt"
        output.write_text(engram_says)
        result = self.sync(self.clone("dev-a"), "down", FAKE_ENGRAM_IMPORT_OUTPUT=str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        return [line for line in result.stderr.splitlines() if "memory of fake" in line]

    BREW_NOTICE = "Update available: 3.5.2 -> 3.6.0\nTo update:\n  brew update && brew upgrade engram\n\n"
    GO_NOTICE = ("Update available: 3.5.2 -> 3.6.0\nTo update:\n  go install github.com/x/engram@latest\n"
                 "  or: https://github.com/x/releases/latest\n\n")

    def test_the_import_line_is_the_result_when_engram_prints_its_brew_update_notice_first(self):
        lines = self.import_summary(self.BREW_NOTICE + "No new chunks to import.\n(143 chunks already imported)\n")

        self.assertEqual(lines, ["[engram-memory] memory of fake: No new chunks to import."])

    def test_the_import_line_is_the_result_when_engram_prints_its_go_install_notice_first(self):
        lines = self.import_summary(self.GO_NOTICE + "Imported 3 new chunk(s) from .engram/\n")

        self.assertEqual(lines, ["[engram-memory] memory of fake: Imported 3 new chunk(s) from .engram/"])

    def test_with_no_result_line_the_first_line_outside_the_notice_is_shown(self):
        lines = self.import_summary(self.GO_NOTICE + "Sync done.\n")

        self.assertEqual(lines, ["[engram-memory] memory of fake: Sync done."])

    def test_an_import_with_no_notice_shows_its_result(self):
        lines = self.import_summary("Imported 2 new chunk(s) from .engram/\n")

        self.assertEqual(lines, ["[engram-memory] memory of fake: Imported 2 new chunk(s) from .engram/"])

    def test_what_others_already_published_is_never_rewritten_by_this_machines_cleaning(self):
        # A cloud machine has home /root: cleaning every chunk with ITS identifiers would rewrite other people's.
        (self.home / ".config" / "engram-memory-tools" / "patterns.json").write_text(json.dumps(dict(PATTERNS, home_path="/root")))
        publisher = self.clone("publisher")
        old = write_chunk(publisher, {"sessions": [], "observations": [{"id": 1, "sync_id": "obs-old", "type": "decision", "title": "old", "content": "installed under /root/go/bin", "session_id": "s"}], "prompts": [], "mutations": []})
        git("add", "-A", cwd=publisher)
        git("commit", "-m", "published by another machine", cwd=publisher)
        git("push", "origin", "main", cwd=publisher)
        before = self.remote_bytes(f".engram/chunks/{old}.jsonl.gz")
        machine = self.clone("cloud-like")

        result = self.sync(machine, "up", FAKE_ENGRAM_TEXT="a new note")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_bytes(f".engram/chunks/{old}.jsonl.gz"), before)
        manifest_ids = [c["id"] for c in json.loads(self.remote_file(".engram/manifest.json"))["chunks"]]
        self.assertIn(old, manifest_ids)
        self.assertEqual(len(manifest_ids), 2)

    def test_a_machine_with_no_git_identity_still_shares_signed_as_engram_memory(self):
        (self.home / ".gitconfig").unlink()
        target = self.root / "no-identity"
        git("clone", str(self.remote), str(target))

        result = self.sync(target, "up", FAKE_ENGRAM_TEXT="knowledge from a machine with no identity")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shared", result.stderr)
        author = git("--git-dir", str(self.remote), "log", "-1", "--format=%an", "main").strip()
        self.assertEqual(author, "Engram memory")

    def test_a_git_identity_that_exists_is_never_replaced(self):
        a = self.clone("dev-a")

        self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from a real person")

        self.assertEqual(git("--git-dir", str(self.remote), "log", "-1", "--format=%an", "main").strip(), "Dev One")

    def test_memory_left_uncommitted_by_a_previous_run_is_finished_by_the_next_one(self):
        a = self.clone("dev-a")
        self.sync(a, "up", FAKE_ENGRAM_TEXT="first", ENGRAM_MEMORY_DRY_RUN="1")  # leaves a cleaned chunk uncommitted
        self.assertNotEqual(git("status", "--porcelain", cwd=a), "")

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="second")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_observation_texts(), {"first", "second"})
        self.assertEqual(git("status", "--porcelain", cwd=a), "")

    def test_nothing_new_means_no_commit(self):
        a = self.clone("dev-a")
        before = git("rev-parse", "HEAD", cwd=a)

        result = self.sync(a, "up", FAKE_ENGRAM_NOTHING="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git("rev-parse", "HEAD", cwd=a), before)

    # --- a chunk this machine has not published yet is never imported back over the local memory ----------------

    def test_the_chunk_a_machine_shares_is_recorded_as_imported_and_the_next_down_imports_nothing(self):
        a, machine = self.clone("dev-a"), self.engram_machine("a")

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A", **machine)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shared", result.stderr)
        (chunk,) = self.manifest_ids(a)
        self.assertEqual(self.marked_imported(machine), {chunk})
        self.reset_import_log(machine)

        again = self.sync(a, "down", **machine)

        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.import_runs(machine), (1, []))

    def test_the_record_may_wait_for_the_database_longer_than_the_network_limit(self):
        # ENGRAM_MEMORY_TIMEOUT bounds the network calls; the record has its own wait (ENGRAM_MEMORY_DB_TIMEOUT)
        a, machine = self.clone("dev-a"), self.engram_machine("a")
        self.install_timeout_that_really_ends_commands()
        locked = self.root / "locked-by-engram-3s"

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A", FAKE_ENGRAM_LOCK_AFTER_EXPORT=str(locked), ENGRAM_MEMORY_TIMEOUT="2", **machine)

        self.assertEqual(result.returncode, 0, result.stderr)
        (chunk,) = self.manifest_ids(a)
        self.assertEqual(self.marked_imported(machine), {chunk}, result.stderr)
        self.assertNotIn("could not record", result.stderr)

    def test_a_request_to_end_that_comes_before_the_pid_of_the_command_is_known_still_ends_it(self):
        # The window between starting the command and knowing its pid is microseconds long: it is widened here with
        # a copy of the script that sleeps in it, and the request arrives while the copy sleeps.
        clone = self.clone("dev-a")
        original = (clone / "tools" / "engram-memory-sync.sh").read_text()
        self.assertEqual(original.count("  CHILD=$!\n"), 1)
        (clone / "tools" / "engram-memory-sync-slow-start.sh").write_text(original.replace("  CHILD=$!\n", "  sleep 1\n  CHILD=$!\n"))
        process, engram_pid = self.start_hanging_sync(clone, script="engram-memory-sync-slow-start.sh")

        self.end_and_check(process, engram_pid, clone, signal.SIGTERM)

    @unittest.skipIf(WINDOWS, "file modes do not exist on Windows: Python always reports 777 for a folder")
    def test_the_lock_folder_is_private_to_the_user(self):
        clone = self.clone("dev-a")
        process, engram_pid = self.start_hanging_sync(clone)

        mode = (clone / ".engram" / ".sync.lock").stat().st_mode & 0o777
        self.end_and_check(process, engram_pid, clone, signal.SIGTERM)

        self.assertEqual(oct(mode), oct(0o700))

    def test_without_pgrep_the_sync_still_ends_what_it_waits_for_and_frees_its_lock(self):
        clone = self.clone("dev-a")
        farm = self.root / "path-without-pgrep"
        farm.mkdir()
        fill_windows_farm(farm, lambda name: name in ("pgrep", "timeout", "gtimeout"))
        for directory in [] if WINDOWS else os.environ["PATH"].split(os.pathsep):
            if not os.path.isdir(directory):
                continue
            for entry in os.scandir(directory):
                try:
                    runnable = entry.name not in ("pgrep", "timeout", "gtimeout") and entry.is_file() and os.access(entry.path, os.X_OK)
                except OSError:
                    continue
                if runnable and not os.path.lexists(farm / entry.name):
                    os.symlink(entry.path, farm / entry.name)
        self.env["PATH"] = f"{self.bin_dir}{os.pathsep}{farm}"
        process, engram_pid = self.start_hanging_sync(clone)

        self.end_and_check(process, engram_pid, clone, signal.SIGTERM)

    def test_the_chunk_is_recorded_as_imported_also_when_a_timeout_program_is_installed(self):
        a, machine = self.clone("dev-a"), self.engram_machine("a")
        self.install_timeout_that_really_ends_commands()

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A", **machine)

        self.assertEqual(result.returncode, 0, result.stderr)
        (chunk,) = self.manifest_ids(a)
        self.assertEqual(self.marked_imported(machine), {chunk})
        self.assertNotIn("could not record", result.stderr)

    def test_a_chunk_that_an_interrupted_run_left_unshared_is_never_imported_over_the_local_memory(self):
        a, machine = self.clone("dev-a"), self.engram_machine("a")
        # a run made the cleaned chunk but did not share it (a dry run leaves it uncommitted) ...
        self.sync(a, "up", FAKE_ENGRAM_TEXT="mine", ENGRAM_MEMORY_DRY_RUN="1", **machine)
        (chunk,) = self.manifest_ids(a)
        # ... and it was interrupted before it could record the chunk as already imported
        self.forget_marks(machine)
        self.reset_import_log(machine)
        status = git("status", "--porcelain", cwd=a)

        result = self.sync(a, "down", **machine)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marked_imported(machine), {chunk})
        self.assertEqual(self.import_runs(machine), (1, []))
        self.assertEqual(result.stderr.count("memory of fake"), 1)
        self.assertEqual(git("status", "--porcelain", cwd=a), status)

    def test_only_what_other_developers_published_is_imported_when_this_machine_has_an_unshared_chunk(self):
        a, b = self.clone("dev-a"), self.clone("dev-b")
        machine_a, machine_b = self.engram_machine("a"), self.engram_machine("b")
        # B makes a chunk while the remote is unreachable: it is committed, ahead of origin/main, not pushed
        git("remote", "set-url", "origin", str(self.root / "nowhere.git"), cwd=b)
        self.sync(b, "up", FAKE_ENGRAM_TEXT="from B", **machine_b)
        (b_chunk,) = self.manifest_ids(b)
        self.assertNotEqual(git("log", "@{u}..HEAD", "--oneline", cwd=b), "")
        # A shares in the meantime, B gets the remote back, and B's mark of its own chunk was lost
        self.sync(a, "up", FAKE_ENGRAM_TEXT="from A", **machine_a)
        git("remote", "set-url", "origin", str(self.remote), cwd=b)
        self.forget_marks(machine_b)
        self.reset_import_log(machine_b)

        result = self.sync(b, "down", **machine_b)

        self.assertEqual(result.returncode, 0, result.stderr)
        (a_chunk,) = set(self.manifest_ids(b)) - {b_chunk}
        self.assertEqual(self.import_runs(machine_b), (1, [a_chunk]))
        self.assertEqual(self.marked_imported(machine_b), {a_chunk, b_chunk})
        self.assertEqual(len(git("log", "@{u}..HEAD", "--oneline", cwd=b).splitlines()), 1)  # nothing was pushed by a down

    def assert_import_skipped(self, result, a: Path, machine: dict, status: str):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([line for line in result.stderr.splitlines() if line.startswith("[engram-memory]") and "not importing" in line]), 1, result.stderr)
        self.assertNotIn("memory of fake", result.stderr)
        self.assertEqual(self.import_runs(machine), (0, []))
        self.assertEqual(git("status", "--porcelain", cwd=a), status)
        self.assertFalse((a / ".engram" / ".sync.lock").exists())

    def test_when_the_chunk_cannot_be_recorded_because_the_schema_differs_the_import_is_skipped(self):
        a, machine = self.clone("dev-a"), self.engram_machine("a", table=False)
        self.sync(a, "up", FAKE_ENGRAM_TEXT="mine", ENGRAM_MEMORY_DRY_RUN="1", **machine)
        self.reset_import_log(machine)
        status = git("status", "--porcelain", cwd=a)

        result = self.sync(a, "down", **machine)

        self.assert_import_skipped(result, a, machine, status)

    def test_when_the_database_is_locked_the_import_is_skipped_and_the_next_run_records_the_chunk(self):
        a, machine = self.clone("dev-a"), self.engram_machine("a")
        self.sync(a, "up", FAKE_ENGRAM_TEXT="mine", ENGRAM_MEMORY_DRY_RUN="1", **machine)
        (chunk,) = self.manifest_ids(a)
        self.forget_marks(machine)
        self.reset_import_log(machine)
        status = git("status", "--porcelain", cwd=a)
        holder = sqlite3.connect(Path(machine["ENGRAM_DATA_DIR"]) / "engram.db", isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            result = self.sync(a, "down", ENGRAM_MEMORY_DB_TIMEOUT="1", **machine)
        finally:
            holder.execute("ROLLBACK")
            holder.close()

        self.assert_import_skipped(result, a, machine, status)

        retry = self.sync(a, "down", **machine)

        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.marked_imported(machine), {chunk})
        self.assertEqual(self.import_runs(machine), (1, []))

    def test_when_the_new_chunk_cannot_be_recorded_right_after_the_export_it_says_so_once_and_still_shares(self):
        # The chunk is pushed in this very run, so it will not be "unpublished" any more at the next down: this record
        # is the only protection it has, and a failure to write it must not go unnoticed.
        a, machine = self.clone("dev-a"), self.engram_machine("a")
        locked = self.root / "locked-by-engram"

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge from A", FAKE_ENGRAM_LOCK_AFTER_EXPORT=str(locked), ENGRAM_MEMORY_DB_TIMEOUT="1", **machine)

        self.assertEqual(result.returncode, 0, result.stderr)
        warning = "[engram-memory] could not record the new chunk as imported: the next down may re-import it."
        self.assertEqual(result.stderr.splitlines().count(warning), 1, result.stderr)
        self.assertIn("memory of fake shared.", result.stderr)
        self.assertIn("knowledge from A", self.remote_observation_texts())
        self.assertEqual(self.marked_imported(machine), set())  # (waits for the lock to end) nothing was recorded

    # --- nothing waits for a password, and nothing waits forever ------------------------------------------------

    def test_every_network_call_and_every_engram_sync_runs_under_a_time_limit(self):
        for name in ("timeout", "gtimeout"):
            with self.subTest(command=name):
                self.without_timeout_commands(install=name)
                log = self.root / f"{name}.log"
                a = self.clone(f"dev-{name}")

                result = self.sync(a, "up", FAKE_ENGRAM_TEXT=f"knowledge through {name}", FAKE_TIMEOUT_LOG=str(log))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("shared", result.stderr)
                self.assertIn(f"knowledge through {name}", self.remote_observation_texts())
                guard = "-c http.lowSpeedLimit=1000 -c http.lowSpeedTime=20"
                calls = self.timeout_calls(log)
                self.assertTrue(all(limit == "90" for limit, _ in calls), calls)
                commands = [command for _, command in calls]
                self.assertTrue(any(c.startswith("git -C ") and f" {guard} pull --rebase" in c for c in commands), commands)
                self.assertTrue(any(c.startswith("git -C ") and f" {guard} push" in c for c in commands), commands)
                self.assertIn("engram sync --import", commands)
                self.assertIn("engram sync --project fake", commands)
                probes = [call for call in self.all_timeout_calls(log) if "sys.version_info" in call[1]]
                self.assertTrue(probes and all(limit == "10" for limit, _ in probes), probes)  # the probes have their own limit

    def test_the_time_limit_can_be_changed_and_a_value_that_is_not_a_number_is_ignored(self):
        self.without_timeout_commands(install="timeout")
        for value, expected in (("7", "7"), ("soon", "90")):
            with self.subTest(value=value):
                log = self.root / f"limit-{expected}.log"
                a = self.clone(f"dev-{value}")

                result = self.sync(a, "down", ENGRAM_MEMORY_TIMEOUT=value, FAKE_TIMEOUT_LOG=str(log))

                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.timeout_calls(log)
                self.assertGreaterEqual(len(calls), 2)  # the pull and the import
                self.assertEqual({limit for limit, _ in calls}, {expected})

    def expired(self, direction: str, on: str, **extra_env):
        """Runs the script on a fresh clone with the time limit of the calls that contain `on` already expired."""
        self.without_timeout_commands(install="timeout")
        a = self.clone("dev-expired")
        log = self.root / "expired.log"
        result = self.sync(a, direction, FAKE_TIMEOUT_EXPIRE_ON=on, FAKE_TIMEOUT_LOG=str(log), **extra_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([line for line in result.stderr.splitlines() if "timed out" in line]), 1, result.stderr)
        self.assertFalse((a / ".engram" / ".sync.lock").exists())
        self.assertFalse((a / ".git" / "rebase-merge").exists())
        return a, result, [command for _, command in self.timeout_calls(log)]

    def test_a_git_pull_that_times_out_ends_with_one_line_and_leaves_the_clone_as_it_was(self):
        a, result, _ = self.expired("down", " pull ")

        self.assertIn("[engram-memory] git pull timed out after 90s", result.stderr)
        self.assertEqual(git("status", "--porcelain", cwd=a), "")
        self.assertIn("memory of fake", result.stderr)  # the rest of the down went on

    def test_a_git_push_that_times_out_keeps_the_commit_local_and_does_not_try_again(self):
        a, result, commands = self.expired("up", " push ", FAKE_ENGRAM_TEXT="knowledge that cannot be pushed")

        self.assertIn("[engram-memory] push failed (timed out after 90s): the commit stays local", result.stderr)
        self.assertNotIn("memory of fake shared", result.stderr)
        self.assertEqual(len(git("log", "@{u}..HEAD", "--oneline", cwd=a).splitlines()), 1)
        self.assertEqual(self.remote_observation_texts(), set())
        self.assertEqual(len([c for c in commands if " push " in c]), 1)
        self.assertEqual(len([c for c in commands if " pull " in c]), 1)  # the one at the start, none after the timeout
        self.assertEqual(git("status", "--porcelain", cwd=a), "")

    def test_an_engram_import_that_times_out_ends_with_one_line(self):
        a, result, _ = self.expired("down", "sync --import")

        self.assertIn("[engram-memory] engram sync --import timed out after 90s: continuing.", result.stderr)
        self.assertNotIn("memory of fake", result.stderr)
        self.assertEqual(git("status", "--porcelain", cwd=a), "")

    def test_an_engram_export_that_times_out_shares_nothing_and_ends_with_one_line(self):
        a, result, commands = self.expired("up", "sync --project", FAKE_ENGRAM_TEXT="knowledge that is never exported")

        self.assertIn("[engram-memory] engram sync timed out after 90s: not exporting.", result.stderr)
        self.assertEqual(self.remote_observation_texts(), set())
        self.assertEqual(git("status", "--porcelain", cwd=a), "")
        self.assertFalse(any(" push " in c for c in commands))

    def fake_ssh(self, directory: Path, name: str, program: str = "ssh") -> Path:
        """An ssh stand-in in `directory` that never connects and logs "<name>: <arguments>" to FAKE_SSH_LOG."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / program
        path.write_text(FAKE_SSH.replace("@NAME@", name))
        path.chmod(0o755)
        return path

    def ssh_clone(self) -> Path:
        """A clone whose origin is reached over ssh; a plain `ssh` on PATH logs itself as "plain"."""
        a = self.clone("dev-a")
        git("remote", "set-url", "origin", "ssh://git@example.invalid/memory.git", cwd=a)
        self.fake_ssh(self.bin_dir, "plain")
        return a

    def down_over_ssh(self, a: Path, **extra_env) -> list:
        """Runs `down` (its pull goes over ssh and fails) and returns what the ssh stand-ins were called with."""
        log = self.root / "ssh.log"
        result = self.sync(a, "down", FAKE_SSH_LOG=str(log), **extra_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git pull failed", result.stderr)
        calls = log.read_text().splitlines() if log.exists() else []
        self.assertTrue(calls)
        return calls

    def test_ssh_is_told_never_to_prompt_and_never_to_wait_long_to_connect(self):
        calls = self.down_over_ssh(self.ssh_clone())

        for call in calls:
            self.assertTrue(call.startswith("plain: -o BatchMode=yes -o ConnectTimeout=15 "), call)

    def test_an_ssh_command_that_the_user_set_is_kept_and_the_options_are_added_after_it(self):
        mine = self.fake_ssh(self.root / "my ssh", "mine")

        calls = self.down_over_ssh(self.ssh_clone(), GIT_SSH_COMMAND=f"'{mine}' -v")

        for call in calls:
            self.assertTrue(call.startswith("mine: -v -o BatchMode=yes -o ConnectTimeout=15 "), call)

    def test_the_ssh_command_that_the_repository_configures_is_kept_and_gets_the_options(self):
        a = self.ssh_clone()
        configured = self.fake_ssh(self.root / "configured ssh", "configured")
        git("config", "core.sshCommand", f"'{configured}' -v", cwd=a)

        calls = self.down_over_ssh(a)

        for call in calls:
            self.assertTrue(call.startswith("configured: -v -o BatchMode=yes -o ConnectTimeout=15 "), call)

    def test_the_ssh_program_of_git_ssh_is_kept_even_when_its_path_has_spaces_and_gets_the_options(self):
        program = self.fake_ssh(self.root / "git ssh's dir", "gitssh")

        calls = self.down_over_ssh(self.ssh_clone(), GIT_SSH=str(program))

        for call in calls:
            self.assertTrue(call.startswith("gitssh: -o BatchMode=yes -o ConnectTimeout=15 "), call)

    def test_an_ssh_client_that_does_not_understand_dash_o_is_left_as_the_user_configured_it(self):
        # PuTTY's plink takes no `-o BatchMode=yes`: adding it would break a setup that works today.
        a = self.ssh_clone()
        plink = self.fake_ssh(self.root / "putty", "plink", program="plink")
        git("config", "core.sshCommand", f"'{plink}' -ssh", cwd=a)

        calls = self.down_over_ssh(a)

        for call in calls:
            self.assertTrue(call.startswith("plink: "), call)
            self.assertNotIn("BatchMode", call)

    def test_the_options_of_ssh_are_not_added_twice_when_a_pull_restarts_the_script(self):
        stale = self.clone("stale")
        probe = self.root / "ssh-command.txt"
        self.publish_newer_sync_script('  printf \'%s\\n\' "$GIT_SSH_COMMAND" > "$SSH_COMMAND_PROBE"\n')

        result = self.sync(stale, "down", GIT_SSH_COMMAND="myssh -v", SSH_COMMAND_PROBE=str(probe))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NEW SYNC LOGIC", result.stderr)  # the restarted copy is the one that wrote the probe
        self.assertEqual(probe.read_text(), "myssh -v -o BatchMode=yes -o ConnectTimeout=15\n")

    def test_without_timeout_or_gtimeout_everything_runs_unwrapped_and_git_still_gives_up_on_a_dead_connection(self):
        self.without_timeout_commands()
        a = self.clone("dev-a")
        probe = self.root / "push-probe.txt"
        hook = a / ".git" / "hooks" / "pre-push"
        hook.write_text(f'#!/bin/sh\n{{ git config --get http.lowSpeedLimit; git config --get http.lowSpeedTime; }} > "{probe}"\nexit 0\n')
        hook.chmod(0o755)

        result = self.sync(a, "up", FAKE_ENGRAM_TEXT="knowledge shared without a time limit")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shared", result.stderr)
        self.assertIn("knowledge shared without a time limit", self.remote_observation_texts())
        self.assertEqual(probe.read_text().split(), ["1000", "20"])
        self.assertNotIn("timeout", result.stderr)
        self.assertNotIn("not found", result.stderr)
        self.assertEqual(git("status", "--porcelain", cwd=a), "")


if __name__ == "__main__":
    unittest.main()
