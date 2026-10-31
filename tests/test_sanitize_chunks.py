"""Tests of tools/sanitize_chunks.py, the script that makes a chunk safe to share.

Run them from the repository root with:  python3 -m unittest discover -s tests

Every test uses its own temporary HOME with a fake patterns file, so the real configuration of the person who
runs them (and the real ~/.engram/local-only folder) is never read or touched.
"""
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "sanitize_chunks.py"

PATTERNS = {
    "personal_email_patterns": [r"dev@example\.com"],
    "other_identity_patterns": [r"otheridentity"],
    "account": "devaccount",
    "person_name": "Dev Person",
    "username": "devuser",
    "home_path": "/Users/devuser",
}


def load_sanitizer(home: str, patterns: dict):
    """Import the script with HOME pointing to a temporary folder that holds `patterns`."""
    config = Path(home, ".config", "engram-memory-tools")
    config.mkdir(parents=True, exist_ok=True)
    (config / "patterns.json").write_text(json.dumps(patterns))
    # Windows reads USERPROFILE, not HOME: with only HOME set, the script would use the real home of whoever runs the tests.
    previous = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = home
    try:
        spec = importlib.util.spec_from_file_location("sanitize_chunks_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                del os.environ[name]
            else:
                os.environ[name] = value


def observation(content: str, title: str = "a title", kind: str = "decision", n: int = 1) -> dict:
    return {"id": n, "sync_id": f"obs-{n}", "type": kind, "title": title, "content": content, "session_id": "s1"}


def write_chunk(repo: Path, data: dict) -> str:
    """Write one chunk the way Engram does (id = first 8 hex characters of the sha256 of the plain JSON)."""
    plain = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    chunk_id = hashlib.sha256(plain.encode()).hexdigest()[:8]
    (repo / ".engram" / "chunks").mkdir(parents=True, exist_ok=True)
    (repo / ".engram" / "chunks" / f"{chunk_id}.jsonl.gz").write_bytes(gzip.compress(plain.encode()))
    manifest = {"version": 2, "chunks": [{"id": chunk_id, "created_by": "someone", "created_at": "2026-01-01T00:00:00Z", "sessions": 0, "memories": 0, "prompts": 0}]}
    (repo / ".engram" / "manifest.json").write_text(json.dumps(manifest))
    return chunk_id


def add_chunk(repo: Path, data: dict) -> str:
    """Add one more chunk (and its manifest entry) next to the ones `write_chunk` already made."""
    plain = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    chunk_id = hashlib.sha256(plain.encode()).hexdigest()[:8]
    (repo / ".engram" / "chunks" / f"{chunk_id}.jsonl.gz").write_bytes(gzip.compress(plain.encode()))
    manifest_path = repo / ".engram" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["chunks"].append({"id": chunk_id, "created_by": "someone", "created_at": "2026-01-02T00:00:00Z", "sessions": 0, "memories": 0, "prompts": 0})
    manifest_path.write_text(json.dumps(manifest))
    return chunk_id


def relation(n: int, source: str, target: str, reason: str = "") -> dict:
    """A relation mutation the way Engram exports it: the payload is a JSON string."""
    payload = {"sync_id": f"rel-{n}", "source_id": source, "target_id": target, "relation": "related", "reason": reason, "confidence": 0.9, "project": "demo"}
    return {"seq": 0, "target_key": "", "entity": "relation", "entity_key": f"rel-{n}", "op": "upsert", "payload": json.dumps(payload, separators=(",", ":")), "source": "", "project": "demo", "occurred_at": "2026-01-01 10:00:00", "disposition": ""}


def files_under(folder: Path) -> dict:
    """Every file below `folder` with its bytes (empty when the folder does not exist)."""
    if not folder.exists():
        return {}
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def read_only_chunk(repo: Path) -> dict:
    manifest = json.loads((repo / ".engram" / "manifest.json").read_text())
    assert len(manifest["chunks"]) == 1
    path = repo / ".engram" / "chunks" / f"{manifest['chunks'][0]['id']}.jsonl.gz"
    return json.loads(gzip.decompress(path.read_bytes()))


class SanitizeChunksTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = str(Path(self._tmp.name, "home"))
        self.repo = Path(self._tmp.name, "memory")
        self.repo.mkdir()
        self.san = load_sanitizer(self.home, PATTERNS)

    def run_script(self) -> int:
        return self.san.main(self.repo)

    def test_the_script_under_test_uses_only_the_temporary_home(self):
        for path in (self.san.CONFIG, self.san.LOCAL_ONLY):
            self.assertIn(Path(self.home), path.parents)

    def test_a_chunk_with_null_arrays_is_cleaned_not_crashed(self):
        # Engram writes null, not [], for a list with nothing in it (for example no new sessions).
        write_chunk(self.repo, {"sessions": None, "observations": [observation("plain project knowledge")], "prompts": None, "mutations": None})

        self.assertEqual(self.run_script(), 0)

        data = read_only_chunk(self.repo)
        self.assertEqual((data["sessions"], data["prompts"], data["mutations"]), ([], [], []))
        self.assertEqual(data["observations"][0]["content"], "plain project knowledge")

    def test_the_prompts_never_stay(self):
        write_chunk(self.repo, {"sessions": [], "observations": [observation("knowledge")], "prompts": [{"id": 1, "content": "what the person typed"}], "mutations": []})

        self.assertEqual(self.run_script(), 0)

        self.assertEqual(read_only_chunk(self.repo)["prompts"], [])

    def test_lines_about_the_own_accounts_go_and_identifiers_become_placeholders(self):
        content = "\n".join([
            "The booking keeps one deposit per line.",
            "Push only with the personal account devaccount using GH_TOKEN.",
            "Reach me at dev@example.com about it.",
            "Notes of Dev Person in /Users/devuser/work and by devuser.",
            "Another line that mentions otheridentity.",
            "Last project rule.",
        ])
        write_chunk(self.repo, {"sessions": [], "observations": [observation(content)], "prompts": [], "mutations": []})

        self.assertEqual(self.run_script(), 0)

        out = read_only_chunk(self.repo)["observations"][0]["content"]
        self.assertEqual(out.split("\n")[0], "The booking keeps one deposit per line.")
        self.assertEqual(out.split("\n")[-1], "Last project rule.")
        for gone in ("devaccount", "dev@example.com", "Dev Person", "/Users/devuser", "devuser", "otheridentity", "GH_TOKEN"):
            self.assertNotIn(gone, out)
        self.assertIn("user@example.com", out)
        self.assertIn("the author", out)

    def test_it_refuses_and_writes_nothing_when_a_secret_is_left(self):
        original = write_chunk(self.repo, {"sessions": [], "observations": [observation("the key is api_key: abcdefghijklmnop1234567890")], "prompts": [{"id": 1, "content": "x"}], "mutations": []})

        self.assertEqual(self.run_script(), 1)

        self.assertEqual([p.name for p in (self.repo / ".engram" / "chunks").iterdir()], [f"{original}.jsonl.gz"])
        self.assertEqual(json.loads((self.repo / ".engram" / "manifest.json").read_text())["chunks"][0]["id"], original)

    def test_a_reference_to_an_environment_variable_is_not_a_secret(self):
        write_chunk(self.repo, {"sessions": [], "observations": [observation("The signing key is secret: process.env.JWT_SECRET in the config.")], "prompts": [], "mutations": []})

        self.assertEqual(self.run_script(), 0)

    def test_a_second_run_changes_nothing(self):
        write_chunk(self.repo, {"sessions": [], "observations": [observation("Mail dev@example.com about it.")], "prompts": [{"id": 1, "content": "x"}], "mutations": []})
        self.assertEqual(self.run_script(), 0)
        after_first = sorted(p.name for p in (self.repo / ".engram" / "chunks").iterdir())
        manifest = (self.repo / ".engram" / "manifest.json").read_text()

        self.assertEqual(self.run_script(), 0)

        self.assertEqual(sorted(p.name for p in (self.repo / ".engram" / "chunks").iterdir()), after_first)
        self.assertEqual((self.repo / ".engram" / "manifest.json").read_text(), manifest)

    def test_a_person_with_no_other_identities_loses_no_line(self):
        # An empty list of identifiers must match NOTHING: an empty alternation would match every line.
        empty = dict(PATTERNS, personal_email_patterns=[], other_identity_patterns=[])
        san = load_sanitizer(str(Path(self._tmp.name, "other-home")), empty)
        write_chunk(self.repo, {"sessions": [], "observations": [observation("first rule\nsecond rule\nthird rule")], "prompts": [], "mutations": []})

        self.assertEqual(san.main(self.repo), 0)

        self.assertEqual(read_only_chunk(self.repo)["observations"][0]["content"], "first rule\nsecond rule\nthird rule")

    def test_an_email_nobody_listed_is_replaced_too(self):
        # A cloud session cannot know the owner's addresses, so any address that is not an example is masked.
        san = load_sanitizer(str(Path(self._tmp.name, "empty-home")), dict(PATTERNS, personal_email_patterns=[]))

        text, _, _ = san.scrub_text(
            "Ask boss@private.test or user@example.com or noreply@anthropic.com; clone git@github.com:team/repo and react@18.3.1."
        )

        self.assertEqual(
            text,
            "Ask user@example.com or user@example.com or noreply@anthropic.com; clone git@github.com:team/repo and react@18.3.1.",
        )

    def san_for(self, **override):
        return load_sanitizer(str(Path(self._tmp.name, "other-home")), dict(PATTERNS, **override))

    def test_a_short_home_folder_is_masked_only_as_a_whole_path(self):
        # A cloud session's home is /root, which is also the name of Remix's app/root.tsx.
        san = self.san_for(home_path="/root")

        text, _, _ = san.scrub_text(
            "Edit app/root.tsx and /root.tsx; chroot /rootfs; installed under /root/go/bin; saved in /root. Also (/root)."
        )

        self.assertEqual(
            text,
            "Edit app/root.tsx and /root.tsx; chroot /rootfs; installed under ~/go/bin; saved in ~. Also (~).",
        )

    def test_a_short_username_is_masked_only_as_a_whole_word(self):
        san = self.san_for(username="ana", personal_email_patterns=[])

        text, _, _ = san.scrub_text("banana by ana, analysis, ana's note")

        self.assertEqual(text, "banana by user, analysis, user's note")

    def test_a_short_name_and_account_are_masked_only_as_whole_words(self):
        san = self.san_for(person_name="Ana", account="dev")

        text, _, _ = san.scrub_text("Banana said Ana would deploy to devices from dev")

        self.assertEqual(text, "Banana said the author would deploy to devices from <owner-account>")

    def test_the_leftover_check_does_not_flag_a_name_inside_another_word(self):
        san = self.san_for(person_name="Ana", username="ana")
        write_chunk(self.repo, {"sessions": [], "observations": [observation("Banana split")], "prompts": [], "mutations": []})
        original = json.loads(json.dumps(read_only_chunk(self.repo)))

        self.assertEqual(san.main(self.repo), 0)

        self.assertEqual(read_only_chunk(self.repo)["observations"], original["observations"])

    def test_a_chunk_that_is_already_published_is_left_alone_and_only_the_new_one_is_cleaned(self):
        # Another machine's identifiers differ from this one's: cleaning what others published would rewrite it.
        san = self.san_for(home_path="/root", personal_email_patterns=[])
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.test", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.test"}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        published = write_chunk(self.repo, {"sessions": [], "observations": [observation("installed under /root/go/bin", n=1)], "prompts": [], "mutations": []})
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "published"], cwd=self.repo, check=True, env=env)
        published_bytes = (self.repo / ".engram" / "chunks" / f"{published}.jsonl.gz").read_bytes()
        manifest = json.loads((self.repo / ".engram" / "manifest.json").read_text())
        fresh = hashlib.sha256(b"fresh").hexdigest()[:8]
        plain = json.dumps({"sessions": [], "observations": [observation("new note in /root/work", n=2)], "prompts": [{"id": 1, "content": "typed"}], "mutations": []}, separators=(",", ":"))
        fresh = hashlib.sha256(plain.encode()).hexdigest()[:8]
        (self.repo / ".engram" / "chunks" / f"{fresh}.jsonl.gz").write_bytes(gzip.compress(plain.encode()))
        manifest["chunks"].append({"id": fresh, "created_by": "someone", "created_at": "2026-01-02T00:00:00Z", "sessions": 0, "memories": 1, "prompts": 1})
        (self.repo / ".engram" / "manifest.json").write_text(json.dumps(manifest))

        self.assertEqual(san.main(self.repo, only_new=True), 0)

        self.assertEqual((self.repo / ".engram" / "chunks" / f"{published}.jsonl.gz").read_bytes(), published_bytes)
        names = sorted(p.name for p in (self.repo / ".engram" / "chunks").iterdir())
        self.assertIn(f"{published}.jsonl.gz", names)
        self.assertNotIn(f"{fresh}.jsonl.gz", names)  # the new one was cleaned, so it got a new id
        ids = [c["id"] for c in json.loads((self.repo / ".engram" / "manifest.json").read_text())["chunks"]]
        self.assertEqual(len(ids), 2)
        self.assertIn(published, ids)

    def test_the_manifest_does_not_name_who_created_it(self):
        write_chunk(self.repo, {"sessions": [], "observations": [observation("knowledge\nPush only with devaccount here.")], "prompts": [], "mutations": []})

        self.assertEqual(self.run_script(), 0)

        manifest = json.loads((self.repo / ".engram" / "manifest.json").read_text())
        self.assertEqual(manifest["chunks"][0]["created_by"], "owner")

    def test_a_chunk_with_nothing_left_after_cleaning_is_not_kept(self):
        # An export that only carried prompts is empty once they are removed: sharing it would be noise.
        original = write_chunk(self.repo, {"sessions": None, "observations": None, "prompts": [{"id": 1, "content": "only typed text"}], "mutations": None})

        self.assertEqual(self.run_script(), 0)

        self.assertEqual(list((self.repo / ".engram" / "chunks").iterdir()), [])
        self.assertEqual(json.loads((self.repo / ".engram" / "manifest.json").read_text())["chunks"], [])
        self.assertTrue((Path(self.home) / ".engram" / "local-only" / f"{original}.jsonl.gz").exists())

    def test_a_chunk_that_keeps_something_is_kept(self):
        write_chunk(self.repo, {"sessions": None, "observations": None, "prompts": [{"id": 1, "content": "x"}], "mutations": [{"seq": 1, "entity": "session"}]})

        self.assertEqual(self.run_script(), 0)

        self.assertEqual(len(list((self.repo / ".engram" / "chunks").iterdir())), 1)

    # --- the write is all or nothing -------------------------------------------------------------------------

    def two_chunks_to_clean(self):
        first = write_chunk(self.repo, {"sessions": [], "observations": [observation("Mail dev@example.com about it.", n=1)], "prompts": [], "mutations": []})
        second = add_chunk(self.repo, {"sessions": [], "observations": [observation("Also dev@example.com here.", n=2)], "prompts": [], "mutations": []})
        return first, second

    def run_quiet(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.san.main(self.repo)
        return code, out.getvalue()

    def assert_manifest_lists_only_existing_files(self):
        manifest = json.loads((self.repo / ".engram" / "manifest.json").read_text())
        for entry in manifest["chunks"]:
            self.assertTrue((self.repo / ".engram" / "chunks" / f"{entry['id']}.jsonl.gz").exists(), entry["id"])
        return manifest

    def test_a_stop_in_a_later_chunk_writes_and_moves_nothing(self):
        # The first chunk is cleanable, the second one still has a secret after cleaning: the run must STOP
        # before touching anything, so the message "nothing was written" is true.
        first = write_chunk(self.repo, {"sessions": [], "observations": [observation("Mail dev@example.com about it.", n=1)], "prompts": [], "mutations": []})
        second = add_chunk(self.repo, {"sessions": [], "observations": [observation("the key is api_key: abcdefghijklmnop1234567890", n=2)], "prompts": [], "mutations": []})
        before = files_under(self.repo / ".engram")

        code, output = self.run_quiet()

        self.assertEqual(code, 1)
        self.assertIn(f"STOP: chunk {second} still has personal data after cleaning, nothing was written", output)
        self.assertNotIn(f"chunk {first} ->", output)
        self.assertEqual(files_under(self.repo / ".engram"), before)
        self.assertFalse((Path(self.home) / ".engram" / "local-only").exists())

    def test_a_failure_before_the_manifest_is_replaced_leaves_everything_as_it_was_and_a_rerun_finishes(self):
        first, second = self.two_chunks_to_clean()
        manifest_before = (self.repo / ".engram" / "manifest.json").read_bytes()
        real_replace = os.replace

        def disk_full_on_the_manifest(src, dst, *args, **kwargs):
            if Path(dst).name == "manifest.json":
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch("os.replace", side_effect=disk_full_on_the_manifest):
            code, output = self.run_quiet()

        self.assertEqual(code, 1)
        self.assertIn("STOP: could not finish writing", output)
        self.assertEqual((self.repo / ".engram" / "manifest.json").read_bytes(), manifest_before)
        self.assert_manifest_lists_only_existing_files()
        self.assertTrue((self.repo / ".engram" / "chunks" / f"{first}.jsonl.gz").exists())  # the raw files were not moved
        self.assertTrue((self.repo / ".engram" / "chunks" / f"{second}.jsonl.gz").exists())
        self.assertFalse(list((self.repo / ".engram").rglob("*.sanitize-tmp")))  # no half-written file is left behind

        code, _ = self.run_quiet()

        self.assertEqual(code, 0)
        manifest = self.assert_manifest_lists_only_existing_files()
        ids = [entry["id"] for entry in manifest["chunks"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        self.assertNotIn(first, ids)
        self.assertNotIn(second, ids)
        self.assertEqual(sorted(p.name for p in (self.repo / ".engram" / "chunks").iterdir()), sorted(f"{i}.jsonl.gz" for i in ids))
        local_only = Path(self.home) / ".engram" / "local-only"
        self.assertEqual(sorted(p.name for p in local_only.iterdir()), sorted([f"{first}.jsonl.gz", f"{second}.jsonl.gz"]))

    def test_a_failure_while_moving_the_old_files_keeps_the_manifest_true_and_a_rerun_finishes(self):
        first, second = self.two_chunks_to_clean()
        raw = {name: data for name, data in files_under(self.repo / ".engram" / "chunks").items()}
        real_move = shutil.move
        calls = []

        def disk_full_on_the_second_move(src, dst, *args, **kwargs):
            calls.append(src)
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            return real_move(src, dst, *args, **kwargs)

        with mock.patch("shutil.move", side_effect=disk_full_on_the_second_move):
            code, output = self.run_quiet()

        self.assertEqual(code, 1)
        self.assertIn("STOP: could not finish writing", output)
        manifest = self.assert_manifest_lists_only_existing_files()  # never points at a file that was already moved
        done = (self.repo / ".engram" / "manifest.json").read_bytes()
        self.assertEqual(len(manifest["chunks"]), 2)

        code, _ = self.run_quiet()

        self.assertEqual(code, 0)
        self.assertEqual((self.repo / ".engram" / "manifest.json").read_bytes(), done)  # the rerun does not list anything twice
        ids = [entry["id"] for entry in manifest["chunks"]]
        self.assertEqual(sorted(p.name for p in (self.repo / ".engram" / "chunks").iterdir()), sorted(f"{i}.jsonl.gz" for i in ids))
        local_only = files_under(Path(self.home) / ".engram" / "local-only")
        self.assertEqual(local_only, raw)  # the raw originals are all kept, byte for byte, outside the repository

    # --- the sweep of unreferenced chunk files never touches what git has -------------------------------------

    def git_repo_with_two_unreferenced_chunks(self):
        """A git repository with one chunk to clean, a TRACKED chunk of someone else and an UNTRACKED leftover,
        both missing from the manifest. Returns (id to clean, tracked name, untracked name)."""
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.test", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.test"}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        raw = write_chunk(self.repo, {"sessions": [], "observations": [observation("Mail dev@example.com about it.", n=1)], "prompts": [], "mutations": []})
        tracked, untracked = "aaaaaaaa.jsonl.gz", "bbbbbbbb.jsonl.gz"
        (self.repo / ".engram" / "chunks" / tracked).write_bytes(gzip.compress(b'{"sessions":[]}'))
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "someone else's chunk"], cwd=self.repo, check=True, env=env)
        (self.repo / ".engram" / "chunks" / untracked).write_bytes(gzip.compress(b'{"sessions":[]}'))
        return raw, tracked, untracked

    def test_when_git_cannot_report_its_status_the_sweep_is_skipped_and_nothing_tracked_moves(self):
        raw, tracked, untracked = self.git_repo_with_two_unreferenced_chunks()
        chunks = self.repo / ".engram" / "chunks"
        tracked_bytes, untracked_bytes = (chunks / tracked).read_bytes(), (chunks / untracked).read_bytes()

        with mock.patch.object(self.san, "unpublished_chunk_ids", return_value=None):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = self.san.main(self.repo)

        self.assertEqual(code, 0)
        self.assertIn("sweep of unreferenced chunk files skipped: git could not report the status", out.getvalue())
        self.assertEqual((chunks / tracked).read_bytes(), tracked_bytes)
        self.assertEqual((chunks / untracked).read_bytes(), untracked_bytes)
        local_only = Path(self.home) / ".engram" / "local-only"
        self.assertEqual([p.name for p in local_only.iterdir()], [f"{raw}.jsonl.gz"])  # the file this run replaced still moves
        self.assertEqual(len(json.loads((self.repo / ".engram" / "manifest.json").read_text())["chunks"]), 1)

    def test_when_git_reports_its_status_only_the_untracked_leftover_is_swept(self):
        raw, tracked, untracked = self.git_repo_with_two_unreferenced_chunks()
        chunks = self.repo / ".engram" / "chunks"

        code, output = self.run_quiet()

        self.assertEqual(code, 0)
        self.assertNotIn("sweep", output)
        self.assertTrue((chunks / tracked).exists())
        self.assertFalse((chunks / untracked).exists())
        local_only = Path(self.home) / ".engram" / "local-only"
        self.assertEqual(sorted(p.name for p in local_only.iterdir()), sorted([f"{raw}.jsonl.gz", untracked]))

    def test_outside_a_git_repository_every_unreferenced_chunk_is_swept(self):
        write_chunk(self.repo, {"sessions": [], "observations": [observation("plain project knowledge")], "prompts": [], "mutations": []})
        leftover = self.repo / ".engram" / "chunks" / "cccccccc.jsonl.gz"
        leftover.write_bytes(gzip.compress(b'{"sessions":[]}'))

        code, output = self.run_quiet()

        self.assertEqual(code, 0)
        self.assertNotIn("sweep", output)
        self.assertFalse(leftover.exists())
        self.assertTrue((Path(self.home) / ".engram" / "local-only" / "cccccccc.jsonl.gz").exists())

    # --- relations ---------------------------------------------------------------------------------------------

    def test_a_relation_that_points_outside_the_chunk_is_dropped(self):
        # obs-2 is excluded as personal and obs-9 was never in this chunk: no relation may point to either.
        observations = [
            observation("kept knowledge", n=1),
            observation("kept knowledge too", n=3),
            observation("Dev Person prefers to report defects first", kind="preference", n=2),
        ]
        mutations = [relation(1, "obs-1", "obs-3"), relation(2, "obs-1", "obs-2", "about the excluded preference"), relation(3, "obs-9", "obs-1")]
        write_chunk(self.repo, {"sessions": [], "observations": observations, "prompts": [], "mutations": mutations})

        code, output = self.run_quiet()

        self.assertEqual(code, 0)
        self.assertEqual([m["entity_key"] for m in read_only_chunk(self.repo)["mutations"]], ["rel-1"])
        self.assertIn("'relations dropped': 2", output)

    def test_the_reason_of_a_kept_relation_is_blanked(self):
        reason = "Different topic: Dev Person prefers reporting defects vs the project snapshot."
        observations = [observation("kept knowledge", n=1), observation("kept knowledge too", n=2)]
        write_chunk(self.repo, {"sessions": [], "observations": observations, "prompts": [], "mutations": [relation(1, "obs-1", "obs-2", reason)]})

        code, output = self.run_quiet()

        self.assertEqual(code, 0)  # the leftover check still passes: the name in the reason is gone
        kept = read_only_chunk(self.repo)["mutations"]
        self.assertEqual(len(kept), 1)
        payload = json.loads(kept[0]["payload"])
        self.assertEqual(payload["reason"], "")
        self.assertEqual((payload["source_id"], payload["target_id"], payload["relation"], payload["confidence"]), ("obs-1", "obs-2", "related", 0.9))
        self.assertNotIn("Different topic", json.dumps(read_only_chunk(self.repo)))
        self.assertIn("'reasons blanked': 1", output)
        self.assertIn("'relations dropped': 0", output)

    def test_mutations_that_are_not_relations_pass_through_unchanged(self):
        other = {"seq": 4, "entity": "session", "entity_key": "s1", "op": "upsert", "payload": "{\"id\":\"s1\",\"reason\":\"kept as it is\"}"}
        write_chunk(self.repo, {"sessions": [], "observations": [observation("kept knowledge", n=1)], "prompts": [], "mutations": [other]})

        self.assertEqual(self.run_script(), 0)

        self.assertEqual(read_only_chunk(self.repo)["mutations"], [other])


if __name__ == "__main__":
    unittest.main()
