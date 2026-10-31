"""Tests that the tools do not depend on the default text encoding of the machine they run on.

On Windows the default is cp1252, not UTF-8. A tool that opens a file without saying "UTF-8" reads an accented name
as other characters: the privacy file would then list a name that is NOT the one written in the shared memory, so
that name would be neither masked nor found by the final check. These tests run the tools as separate processes under
a default that is not UTF-8:

* `windows_default_env()` makes every text file that is opened without an encoding use cp1252, the way Windows does,
  and keeps the console on cp1252 too.
* the tests that set `LC_ALL=C`, `PYTHONUTF8=0` and `PYTHONCOERCECLOCALE=0` make Python use ASCII for files, for the
  output of a subprocess and for the console, so a tool that reads UTF-8 without saying so fails loudly.

Run them from the repository root with:  python3 -m unittest discover -s tests
"""
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
SANITIZER = TOOLS / "sanitize_chunks.py"
MERGE_MANIFEST = TOOLS / "merge_manifest.py"

PATTERNS = {
    "personal_email_patterns": [r"dev@example\.com"],
    "other_identity_patterns": [r"otheridentity"],
    "account": "devaccount",
    "person_name": "Dev Person",
    "username": "devuser",
    "home_path": "/Users/devuser",
}

# Loaded by Python at start-up (through PYTHONPATH). Text files opened without an encoding use cp1252 here. Python 3.10+
# passes "locale" instead of None when no encoding was given, so both mean "the default of the machine".
CP1252_DEFAULT = '''
import builtins, io


class _OpenWithWindowsDefault:
    # An object, not a function: pathlib keeps io.open as a class attribute, and a function would be bound as a method.
    def __init__(self, real_open):
        self.real_open = real_open

    def __call__(self, file, mode="r", buffering=-1, encoding=None, *args, **kwargs):
        if "b" not in mode and encoding in (None, "locale"):
            encoding = "cp1252"
        return self.real_open(file, mode, buffering, encoding, *args, **kwargs)


builtins.open = io.open = _OpenWithWindowsDefault(io.open)
'''

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Dev",
    "GIT_AUTHOR_EMAIL": "dev@example.com",
    "GIT_COMMITTER_NAME": "Dev",
    "GIT_COMMITTER_EMAIL": "dev@example.com",
}


def clean_env(home: Path) -> dict:
    """The environment of a child process: no Python or locale settings, no git variables (a hook or `git rebase -x`
    would make the git commands of these tests act on another repository), and a home that is ours on every platform
    (Windows reads USERPROFILE, not HOME)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "LC_", "LANG", "GIT_"))}
    env["HOME"] = env["USERPROFILE"] = str(home)
    return env


def write_chunk(repo: Path, data: dict) -> str:
    plain = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    chunk_id = hashlib.sha256(plain.encode("utf-8")).hexdigest()[:8]
    (repo / ".engram" / "chunks").mkdir(parents=True, exist_ok=True)
    (repo / ".engram" / "chunks" / f"{chunk_id}.jsonl.gz").write_bytes(gzip.compress(plain.encode("utf-8")))
    manifest = {"version": 2, "chunks": [{"id": chunk_id, "created_by": "someone", "created_at": "2026-01-01T00:00:00Z", "sessions": 0, "memories": 1, "prompts": 0}]}
    (repo / ".engram" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return chunk_id


def chunk_texts(repo: Path) -> str:
    """Everything that the chunk files of the repository say, as text."""
    return "\n".join(
        gzip.decompress(path.read_bytes()).decode("utf-8") for path in sorted((repo / ".engram" / "chunks").glob("*.jsonl.gz"))
    )


class SanitizerEncodingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="encoding ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.repo = self.root / "memory"
        self.repo.mkdir()
        (self.repo / ".engram").mkdir()
        self.shim = self.root / "shim"
        self.shim.mkdir()
        (self.shim / "sitecustomize.py").write_text(CP1252_DEFAULT, encoding="utf-8")

    def windows_default_env(self, home: Path = None) -> dict:
        env = clean_env(home or self.home)
        env["PYTHONPATH"] = str(self.shim)
        env["PYTHONIOENCODING"] = "cp1252"
        return env

    def write_patterns(self, raw: bytes, home: Path = None):
        folder = (home or self.home) / ".config" / "engram-memory-tools"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "patterns.json").write_bytes(raw)

    def patterns_as_utf8(self, **changes) -> bytes:
        return json.dumps({**PATTERNS, **changes}, ensure_ascii=False).encode("utf-8")

    def sanitize(self, env: dict):
        return subprocess.run([sys.executable, str(SANITIZER), str(self.repo)], env=env, capture_output=True)

    def observation_with(self, text: str) -> dict:
        return {"sessions": [], "observations": [{"id": 1, "sync_id": "obs-1", "type": "decision", "title": "notes", "content": text, "session_id": "s1"}], "prompts": [], "mutations": []}

    def test_an_accented_name_is_masked_even_when_the_machine_default_is_not_utf8(self):
        self.write_patterns(self.patterns_as_utf8(person_name="María Núñez"))
        write_chunk(self.repo, self.observation_with("Meeting notes taken by María Núñez about the new tour."))

        result = self.sanitize(self.windows_default_env())

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace") + result.stdout.decode("utf-8", "replace"))
        text = chunk_texts(self.repo)
        self.assertNotIn("María", text)
        self.assertNotIn("Núñez", text)
        self.assertIn("Meeting notes taken by", text)  # the rest of the knowledge stays

    def test_a_privacy_file_saved_with_a_byte_order_mark_is_read(self):
        # PowerShell 5.1 and some editors write UTF-8 with a BOM.
        self.write_patterns(b"\xef\xbb\xbf" + self.patterns_as_utf8(person_name="María Núñez"))
        write_chunk(self.repo, self.observation_with("Notes by María Núñez."))

        result = self.sanitize(clean_env(self.home))

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertNotIn("María", chunk_texts(self.repo))

    def test_a_privacy_file_that_is_not_utf8_is_refused_with_a_clear_message_and_nothing_changes(self):
        # An editor that saves with the Windows code page writes the "í" of María as the single byte 0xED.
        self.write_patterns(self.patterns_as_utf8(person_name="María Núñez").decode("utf-8").encode("cp1252"))
        write_chunk(self.repo, self.observation_with("Notes by María Núñez."))
        before = {p.name: p.read_bytes() for p in (self.repo / ".engram").rglob("*") if p.is_file()}

        result = self.sanitize(clean_env(self.home))

        stderr = result.stderr.decode("utf-8", "replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", stderr)
        self.assertIn("UTF-8", stderr)
        self.assertIn("patterns.json", stderr)
        after = {p.name: p.read_bytes() for p in (self.repo / ".engram").rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_a_home_folder_that_the_console_cannot_show_does_not_break_the_cleaning_halfway(self):
        # "Ł" is not in cp1252: printing the folder where the original chunk is kept used to raise AFTER the work was done.
        home = self.root / "home Łukasz"
        self.write_patterns(json.dumps(PATTERNS).encode("ascii"), home=home)
        write_chunk(self.repo, {**self.observation_with("plain knowledge"), "prompts": [{"id": 1, "content": "what the person typed"}]})

        result = self.sanitize(self.windows_default_env(home))

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertIn("verification:", result.stdout.decode("utf-8", "replace"))
        self.assertIn("home Łukasz".encode("utf-8"), result.stdout)  # shown as it is, not as an escape
        manifest = json.loads((self.repo / ".engram" / "manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["chunks"]:
            self.assertTrue((self.repo / ".engram" / "chunks" / f"{entry['id']}.jsonl.gz").exists())


    def test_a_manifest_saved_with_a_byte_order_mark_is_read(self):
        self.write_patterns(json.dumps(PATTERNS).encode("ascii"))
        chunk_id = write_chunk(self.repo, self.observation_with("plain knowledge"))
        manifest = {"version": 2, "chunks": [{"id": chunk_id, "created_by": "someone", "created_at": "2026-01-01T00:00:00Z", "sessions": 0, "memories": 1, "prompts": 0}]}
        (self.repo / ".engram" / "manifest.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(manifest).encode("utf-8"))

        result = self.sanitize(clean_env(self.home))

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn("plain knowledge", chunk_texts(self.repo))

    def test_a_privacy_file_that_is_not_valid_json_is_refused_with_a_clear_message_and_nothing_changes(self):
        self.write_patterns(b'{"person_name": ')
        write_chunk(self.repo, self.observation_with("Notes by Dev Person."))
        before = {p.name: p.read_bytes() for p in (self.repo / ".engram").rglob("*") if p.is_file()}

        result = self.sanitize(clean_env(self.home))

        stderr = result.stderr.decode("utf-8", "replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", stderr)
        self.assertIn("not valid JSON", stderr)
        self.assertEqual({p.name: p.read_bytes() for p in (self.repo / ".engram").rglob("*") if p.is_file()}, before)

    def test_a_manifest_with_an_accented_author_is_read_when_the_machine_default_is_not_utf8(self):
        # The manifest that Engram writes carries the characters as they are, and git reports on that repository too.
        self.write_patterns(json.dumps(PATTERNS).encode("ascii"))
        chunk_id = write_chunk(self.repo, self.observation_with("plain knowledge"))
        manifest = {"version": 2, "chunks": [{"id": chunk_id, "created_by": "José Peña", "created_at": "2026-01-01T00:00:00Z", "sessions": 0, "memories": 1, "prompts": 0}]}
        (self.repo / ".engram" / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        env = clean_env(self.home)
        env.update({"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"})  # files, subprocesses and console: ASCII

        result = self.sanitize(env)

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn("plain knowledge", chunk_texts(self.repo))


class MergeManifestEncodingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="encoding ")
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "memory"
        (self.repo / ".engram").mkdir(parents=True)

    def git(self, *args, check=True):
        env = {**clean_env(Path(self._tmp.name)), **GIT_IDENTITY}
        return subprocess.run(["git", "-C", str(self.repo), *args], env=env, capture_output=True, check=check)

    def save_manifest(self, ids_and_authors):
        manifest = {"version": 2, "chunks": [{"id": i, "created_by": who, "created_at": "2026-01-01T00:00:00Z", "sessions": 0, "memories": 1, "prompts": 0} for i, who in ids_and_authors]}
        # Engram writes the characters as they are (ensure_ascii=False in spirit): the file holds raw UTF-8.
        (self.repo / ".engram" / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def conflict_between_two_developers(self):
        self.git("init", "-q", "-b", "main")
        self.save_manifest([("aaaaaaaa", "owner")])
        self.commit("first")
        self.git("switch", "-q", "-c", "other")
        self.save_manifest([("aaaaaaaa", "owner"), ("bbbbbbbb", "José Peña")])
        self.commit("other developer")
        self.git("switch", "-q", "main")
        self.save_manifest([("aaaaaaaa", "owner"), ("cccccccc", "Zoë")])
        self.commit("this developer")
        merged = self.git("merge", "-q", "other", check=False)
        self.assertNotEqual(merged.returncode, 0, "the test needs a real conflict on the manifest")

    def test_the_merged_manifest_has_lf_line_breaks_on_every_platform(self):
        self.git("init", "-q", "-b", "main")
        self.save_manifest([("aaaaaaaa", "owner")])
        self.commit("first")
        self.git("switch", "-q", "-c", "other")
        self.save_manifest([("aaaaaaaa", "owner"), ("bbbbbbbb", "Zoe")])
        self.commit("other developer")
        self.git("switch", "-q", "main")
        self.save_manifest([("aaaaaaaa", "owner"), ("cccccccc", "Ana")])
        self.commit("this developer")
        self.assertNotEqual(self.git("merge", "-q", "other", check=False).returncode, 0)

        result = subprocess.run([sys.executable, str(MERGE_MANIFEST), str(self.repo)], env=clean_env(Path(self._tmp.name)), capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"\r", (self.repo / ".engram" / "manifest.json").read_bytes())

    def test_a_manifest_side_with_only_unicode_blanks_counts_as_empty(self):
        for blank in (b"\xc2\xa0\n", b"\x1c\n"):  # blanks for text, not for bytes
            with self.subTest(blank=blank):
                shutil.rmtree(self.repo, ignore_errors=True)
                (self.repo / ".engram").mkdir(parents=True)
                self.git("init", "-q", "-b", "main")
                self.save_manifest([("aaaaaaaa", "owner")])
                self.commit("first")
                self.git("switch", "-q", "-c", "other")
                (self.repo / ".engram" / "manifest.json").write_bytes(blank)
                self.commit("other developer")
                self.git("switch", "-q", "main")
                self.save_manifest([("aaaaaaaa", "owner"), ("cccccccc", "Ana")])
                self.commit("this developer")
                self.assertNotEqual(self.git("merge", "-q", "other", check=False).returncode, 0)

                result = subprocess.run([sys.executable, str(MERGE_MANIFEST), str(self.repo)], env=clean_env(Path(self._tmp.name)), capture_output=True)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([c["id"] for c in json.loads((self.repo / ".engram" / "manifest.json").read_bytes())["chunks"]], ["aaaaaaaa", "cccccccc"])

    def test_a_side_that_git_cannot_show_counts_as_empty(self):
        self.git("init", "-q", "-b", "main")  # no conflict: `git show :2:` and `:3:` exit with an error

        result = subprocess.run([sys.executable, str(MERGE_MANIFEST), str(self.repo)], env=clean_env(Path(self._tmp.name)), capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.repo / ".engram" / "manifest.json").read_bytes())["chunks"], [])

    def test_a_manifest_side_that_is_not_utf8_is_not_merged_with_replacement_characters(self):
        self.git("init", "-q", "-b", "main")
        self.save_manifest([("aaaaaaaa", "owner")])
        self.commit("first")
        self.git("switch", "-q", "-c", "other")
        self.save_manifest([("aaaaaaaa", "owner"), ("bbbbbbbb", "Zoe")])
        path = self.repo / ".engram" / "manifest.json"
        path.write_bytes(path.read_bytes().replace(b"Zoe", b"Zo\xeb"))  # a Windows editor saved the "e with diaeresis" as one byte
        self.commit("other developer")
        self.git("switch", "-q", "main")
        self.save_manifest([("aaaaaaaa", "owner"), ("cccccccc", "Ana")])
        self.commit("this developer")
        self.assertNotEqual(self.git("merge", "-q", "other", check=False).returncode, 0)
        before = path.read_bytes()

        result = subprocess.run([sys.executable, str(MERGE_MANIFEST), str(self.repo)], env=clean_env(Path(self._tmp.name)), capture_output=True)

        self.assertNotEqual(result.returncode, 0)
        stderr = result.stderr.decode("utf-8", "replace")
        self.assertNotIn("Traceback", stderr)
        self.assertIn("not UTF-8", stderr)
        self.assertEqual(path.read_bytes(), before)  # the conflict is still there for a person to solve
        self.assertNotIn(b"\xef\xbf\xbd", path.read_bytes())

    def test_two_manifests_with_accented_authors_are_merged_when_the_machine_default_is_not_utf8(self):
        self.conflict_between_two_developers()
        env = clean_env(Path(self._tmp.name))
        env.update({"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"})  # files, subprocesses and console: ASCII

        result = subprocess.run([sys.executable, str(MERGE_MANIFEST), str(self.repo)], env=env, capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        merged = json.loads((self.repo / ".engram" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({c["id"]: c["created_by"] for c in merged["chunks"]}, {"aaaaaaaa": "owner", "bbbbbbbb": "José Peña", "cccccccc": "Zoë"})


if __name__ == "__main__":
    unittest.main()
