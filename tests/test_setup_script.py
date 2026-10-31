"""Tests of tools/setup.sh --auto, which writes the local privacy file the cleaning script reads.

A cloud session or a CI machine commits as a bot, and its user is `root` or `user`. Taking those as the identity of
a person would blank out ordinary words in the shared memory ("Claude Code", "root cause") and, worse, would not
know the real owner's data. Run with:  python3 -m unittest discover -s tests

Every test uses its own temporary HOME and runs outside any git repository, so the identity of whoever runs the
tests is never read.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_sanitize_chunks import load_sanitizer
from windows_support import WINDOWS, link_command

SETUP = Path(__file__).resolve().parent.parent / "tools" / "setup.sh"


class SetupAutoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name, "home")
        self.home.mkdir()
        self.outside = Path(self._tmp.name, "not-a-repository")
        self.outside.mkdir()
        # The script learns things from the repository it lives in: run a copy that has no history and no origin,
        # never the real memory repository of whoever runs the tests.
        mem = Path(self._tmp.name, "memory")
        (mem / "tools").mkdir(parents=True)
        shutil.copy(SETUP, mem / "tools" / "setup.sh")
        shutil.copy(SETUP.parent / "find-python.sh", mem / "tools" / "find-python.sh")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=mem, check=True, capture_output=True)
        self.setup_copy = mem / "tools" / "setup.sh"

    def run_setup(self, name: str, email: str, user: str, **extra: str) -> dict:
        (self.home / ".gitconfig").write_text(f"[user]\n\tname = {name}\n\temail = {email}\n")
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),  # Windows reads this one, not HOME
            "USER": user,
            "GIT_CONFIG_NOSYSTEM": "1",
            **extra,
        }
        done = subprocess.run(["bash", str(self.setup_copy), "--auto"], env=env, cwd=self.outside, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads((self.home / ".config" / "engram-memory-tools" / "patterns.json").read_text())

    def test_it_finds_the_python_that_works_when_python3_is_only_the_store_shortcut(self):
        shortcut = Path(self._tmp.name, "windows-bin")
        shortcut.mkdir()
        (shortcut / "python3").write_text("#!/bin/sh\necho 'Python was not found; run without arguments to install from the Microsoft Store.' >&2\nexit 49\n")
        (shortcut / "python3").chmod(0o755)
        link_command(sys.executable, shortcut / "python")

        patterns = self.run_setup("Ana Lopez", "ana@example.org", "ana", PATH=f"{shortcut}{os.pathsep}{os.environ['PATH']}")

        self.assertEqual(patterns["person_name"], "Ana Lopez")

    def test_the_privacy_file_is_never_readable_by_others_not_even_for_a_moment(self):
        # A wrapper around Python looks at the file right after it was written, before setup.sh restricts it.
        seen = Path(self._tmp.name, "mode-when-written")
        wrapper = Path(self._tmp.name, "wrapper")
        wrapper.write_text(f'#!/bin/sh\n"{sys.executable}" "$@"\ncode=$?\nstat -c %a "$HOME/.config/engram-memory-tools/patterns.json" > "{seen}" 2>/dev/null || stat -f %Lp "$HOME/.config/engram-memory-tools/patterns.json" > "{seen}"\nexit $code\n')
        wrapper.chmod(0o755)
        old_umask = os.umask(0o022)  # an ordinary umask: the file would be created readable by others
        try:
            self.run_setup("Ana Lopez", "ana@example.org", "ana", ENGRAM_MEMORY_PYTHON=str(wrapper))
        finally:
            os.umask(old_umask)

        self.assertEqual(seen.read_text().strip(), "600")

    @unittest.skipIf(WINDOWS, "file modes do not exist on Windows: Python always reports 666 for a file")
    def test_an_existing_privacy_file_that_others_can_read_is_tightened(self):
        folder = self.home / ".config" / "engram-memory-tools"
        folder.mkdir(parents=True)
        (folder / "patterns.json").write_text("{}")
        (folder / "patterns.json").chmod(0o644)
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "USERPROFILE": str(self.home), "USER": "ana", "GIT_CONFIG_NOSYSTEM": "1"}

        done = subprocess.run(["bash", str(self.setup_copy), "--auto"], env=env, cwd=self.outside, capture_output=True, text=True)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(oct((folder / "patterns.json").stat().st_mode & 0o777), "0o600")

    def test_the_python_chosen_with_the_variable_is_the_one_that_is_used(self):
        log = Path(self._tmp.name, "chosen-python-was-used")
        chosen = Path(self._tmp.name, "my python")
        chosen.write_text(f'#!/bin/sh\necho used >> "{log}"\nexec "{sys.executable}" "$@"\n')
        chosen.chmod(0o755)

        patterns = self.run_setup("Ana Lopez", "ana@example.org", "ana", ENGRAM_MEMORY_PYTHON=str(chosen))

        self.assertEqual(patterns["person_name"], "Ana Lopez")
        self.assertIn("used", log.read_text())

    def test_without_its_helper_it_says_so_by_name_and_changes_nothing(self):
        self.setup_copy.with_name("find-python.sh").unlink()
        (self.home / ".gitconfig").write_text("[user]\n\tname = Ana Lopez\n\temail = ana@example.org\n")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "USERPROFILE": str(self.home), "USER": "ana", "GIT_CONFIG_NOSYSTEM": "1"}

        done = subprocess.run(["bash", str(self.setup_copy), "--auto"], env=env, cwd=self.outside, capture_output=True, text=True)

        self.assertNotEqual(done.returncode, 0)
        self.assertIn("find-python.sh is missing", done.stderr)
        self.assertIn("git pull", done.stderr)
        self.assertNotIn("No such file", done.stderr)
        self.assertFalse((self.home / ".config" / "engram-memory-tools" / "patterns.json").exists())

    def test_without_a_working_python_it_says_so_before_asking_anything_and_changes_nothing(self):
        (self.home / ".gitconfig").write_text("[user]\n\tname = Ana Lopez\n\temail = ana@example.org\n")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "USERPROFILE": str(self.home), "USER": "ana", "GIT_CONFIG_NOSYSTEM": "1", "ENGRAM_MEMORY_PYTHON": str(self.home / "no-such-python")}

        done = subprocess.run(["bash", str(self.setup_copy)], env=env, cwd=self.outside, capture_output=True, text=True, input="")

        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no working Python", done.stderr)
        self.assertIn("ENGRAM_MEMORY_PYTHON", done.stderr)
        self.assertFalse((self.home / ".config" / "engram-memory-tools" / "patterns.json").exists())

    def test_the_identity_of_a_person_is_used(self):
        cfg = self.run_setup("Dev Person", "dev@corp.test", "devuser")

        self.assertEqual(cfg["person_name"], "Dev Person")
        self.assertEqual(cfg["username"], "devuser")
        self.assertEqual(cfg["personal_email_patterns"], [r"dev@corp\.test"])
        # the shell of Windows hands the home to Python as C:/Users/..., the same folder written with forward slashes
        self.assertEqual(Path(cfg["home_path"]) if WINDOWS else cfg["home_path"], self.home if WINDOWS else str(self.home))

    def test_a_bot_and_a_system_user_are_not_taken_as_a_person(self):
        cfg = self.run_setup("Claude", "noreply@anthropic.com", "root")

        self.assertEqual(cfg["person_name"], "no-name-given")
        self.assertEqual(cfg["username"], "no-username-given")
        self.assertEqual(cfg["personal_email_patterns"], [])

    def test_ordinary_words_survive_the_privacy_file_of_a_cloud_session(self):
        cfg = self.run_setup("Claude", "noreply@anthropic.com", "root")
        san = load_sanitizer(str(Path(self._tmp.name, "sanitizer-home")), cfg)

        text, _, _ = san.scrub_text("Claude Code found the root cause of the user bug.")

        self.assertEqual(text, "Claude Code found the root cause of the user bug.")

    def test_the_owner_can_name_their_identifiers_for_a_machine_that_does_not_know_them(self):
        cfg = self.run_setup(
            "Claude",
            "noreply@anthropic.com",
            "root",
            ENGRAM_MEMORY_NAME="Real Owner",
            ENGRAM_MEMORY_EMAILS="owner@one.test,owner@two.test",
            ENGRAM_MEMORY_ACCOUNT="ownaccount",
        )

        self.assertEqual(cfg["person_name"], "Real Owner")
        self.assertEqual(cfg["account"], "ownaccount")
        self.assertEqual(cfg["personal_email_patterns"], [r"owner@one\.test", r"owner@two\.test"])


class SetupHintsTest(unittest.TestCase):
    """A machine that does not know the owner learns the team account and the name from the memory repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        (self.home / ".gitconfig").write_text("[user]\n\tname = Claude\n\temail = noreply@anthropic.com\n")
        self.mem = root / "memory"
        (self.mem / "tools").mkdir(parents=True)
        shutil.copy(SETUP, self.mem / "tools" / "setup.sh")
        shutil.copy(SETUP.parent / "find-python.sh", self.mem / "tools" / "find-python.sh")
        self.git("init", "-q", "-b", "main")

    def git(self, *args, author: str = "Seed"):
        env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": "a@b.test", "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": "a@b.test", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.home), "USERPROFILE": str(self.home)}
        subprocess.run(["git", *args], cwd=self.mem, env=env, check=True, capture_output=True)

    def commits(self, author: str, count: int):
        for _ in range(count):
            self.git("commit", "-q", "--allow-empty", "-m", "memory", author=author)

    def run_setup(self, user: str = "root", **extra: str) -> dict:
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "USERPROFILE": str(self.home), "USER": user, "GIT_CONFIG_NOSYSTEM": "1", **extra}
        done = subprocess.run(["bash", str(self.mem / "tools" / "setup.sh"), "--auto"], env=env, cwd=self.mem.parent, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads((self.home / ".config" / "engram-memory-tools" / "patterns.json").read_text())

    def test_the_account_is_the_owner_of_the_memory_repository_and_credentials_are_dropped(self):
        self.git("remote", "add", "origin", "https://user:secret@github.com/ownerteam/engram-fake.git")

        cfg = self.run_setup()

        self.assertEqual(cfg["account"], "ownerteam")
        self.assertNotIn("secret", json.dumps(cfg))

    def test_an_scp_like_address_gives_the_account_too(self):
        self.git("remote", "add", "origin", "git@github.com:ownerteam/engram-fake.git")

        self.assertEqual(self.run_setup()["account"], "ownerteam")

    def test_a_local_path_is_not_an_account(self):
        self.git("remote", "add", "origin", "/tmp/some/place/remote.git")

        self.assertEqual(self.run_setup()["account"], "no-account-given")

    def test_the_name_is_the_most_frequent_author_that_is_not_generic(self):
        self.commits("Claude", 4)
        self.commits("Engram memory", 3)
        self.commits("Real Owner", 2)
        self.commits("Someone Else", 1)

        self.assertEqual(self.run_setup()["person_name"], "Real Owner")

    def test_a_real_identity_of_the_machine_beats_the_hint(self):
        (self.home / ".gitconfig").write_text("[user]\n\tname = Dev Person\n\temail = dev@corp.test\n")
        self.commits("Real Owner", 3)

        self.assertEqual(self.run_setup(user="devuser")["person_name"], "Dev Person")

    def test_the_variables_of_the_owner_beat_the_hints(self):
        self.git("remote", "add", "origin", "https://github.com/ownerteam/engram-fake.git")
        self.commits("Real Owner", 3)

        cfg = self.run_setup(ENGRAM_MEMORY_NAME="Named Owner", ENGRAM_MEMORY_ACCOUNT="namedaccount")

        self.assertEqual((cfg["person_name"], cfg["account"]), ("Named Owner", "namedaccount"))


if __name__ == "__main__":
    unittest.main()
