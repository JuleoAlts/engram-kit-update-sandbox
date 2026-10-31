"""Tests of the hardening of the sanitizer: what it must catch, what must never hang it, and what must never end in a
traceback.

Run them from the repository root with:  python3 -m unittest discover -s tests
Every test uses its own temporary HOME with a fake privacy file. The secrets below are assembled at run time from
pieces so that this file does not hold a string that a secret scanner would take for a real one.
"""
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_unicode_forms import PATTERNS, load_sanitizer, observation, write_chunk

TOOLS = Path(__file__).resolve().parent.parent / "tools"
SANITIZER = TOOLS / "sanitize_chunks.py"
MERGE_MANIFEST = TOOLS / "merge_manifest.py"


def secret(*pieces: str) -> str:
    return "".join(pieces)


class SanitizerHardeningTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hardening ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.n = 0

    def san_for(self, **changes):
        self.n += 1
        return load_sanitizer(str(self.root / f"home{self.n}"), {**PATTERNS, **changes})

    # --- a line must never hang the cleaning -----------------------------------------------------------------

    def test_a_very_long_line_full_of_trigger_words_is_cleaned_in_a_moment(self):
        san = self.san_for()
        line = " ".join(["token"] * 40000)  # 240 KB on one line: this used to take 15 seconds, and 100 s at 2 MB

        started = time.monotonic()
        san.scrub_text(line)

        self.assertLess(time.monotonic() - started, 5)

    def test_a_line_about_the_account_and_a_token_is_still_dropped_when_they_are_close(self):
        san = self.san_for()

        text, dropped, _ = san.scrub_text("keep this\nthe devaccount token is used here\nand keep this too")

        self.assertEqual((text, dropped), ("keep this\nand keep this too", 1))

    def test_an_account_far_from_a_trigger_word_is_masked_but_the_line_is_kept(self):
        san = self.san_for()

        text, dropped, _ = san.scrub_text("devaccount " + "x" * 400 + " push")

        self.assertEqual((text, dropped), ("<owner-account> " + "x" * 400 + " push", 0))

    # --- addresses ---------------------------------------------------------------------------------------------

    def test_a_git_address_is_kept_only_for_the_known_hosts(self):
        san = self.san_for(personal_email_patterns=[])

        text, _, _ = san.scrub_text("clone git@github.com:team/repo.git or git@gitlab.com:x/y or git@internal-host.corp:team/repo.git")

        self.assertEqual(text, "clone git@github.com:team/repo.git or git@gitlab.com:x/y or user@example.com:team/repo.git")

    def test_an_address_under_a_subdomain_of_example_is_not_a_harmless_one(self):
        san = self.san_for(personal_email_patterns=[])

        text, _, _ = san.scrub_text("write to ana@sub.example.com or to ana@example.com or ana@notexample.com")

        self.assertEqual(text, "write to user@example.com or to ana@example.com or user@example.com")

    # --- what the final check must see ---------------------------------------------------------------------------

    def test_a_home_folder_that_is_left_stops_the_cleaning(self):
        san = self.san_for(home_path="/home/zed-work", username="someoneelse")

        self.assertEqual(san.leftovers("see /home/zed-work/app"), {"home path": 1})
        self.assertEqual(san.leftovers("see /home/zed-work-other/app"), {})

    def test_the_secrets_that_used_to_pass_are_found(self):
        san = self.san_for()
        shapes = {
            "private key block": secret("-----BEGIN ", "RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu"),
            "private key block (openssh)": secret("-----BEGIN ", "OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW"),
            "private key block (pgp, with header lines)": secret("-----BEGIN ", "PGP PRIVATE KEY BLOCK-----\nVersion: GnuPG v2\n\nlQOYBF5zxkMBCADzbqlsJdcc9UNT6U0mwcTfw0cTzZkzWm7Q6D8C1y3b2oB4kE9"),
            "private key block (escaped line breaks)": secret("-----BEGIN ", "PRIVATE KEY-----\\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj"),
            "url with a password, a host without a dot": secret("postgres://", "admin:", "Xk9fQ2mL8vN3pR7s", "@db:5432/app"),
            "url with a random password and a scheme with a dot": secret("amqps://", "svc:", "q7ZkR2vLm9XaT4pW", "@broker:5671/vh"),
            "stripe webhook secret": secret("whsec_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"),
            "azure shared access key": secret("SharedAccessKey=", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6A7b8C9d0=="),
            "azure storage key in lower case": secret("accountkey=", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6A7b8C9d0=="),
            "aws secret assignment in json": secret('"aws_secret_access_key": "', "wJalrXUtnFEMIK7MDENGbPxRfiCYQ7XvT2nLw9sA", '"'),
            "url with a password": secret("postgres://", "admin:", "Xk9fQ2mL8vN3pR7s", "@db.internal:5432/app"),
            "stripe key": secret("sk_", "live_", "a1B2c3D4e5F6g7H8i9J0k1L2"),
            "google api key": secret("AIza", "SyA1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q"),
            "slack webhook": secret("https://hooks.slack.com/services/", "T01ABCDEF", "/B02GHIJKL/", "a1b2c3d4e5f6g7h8i9j0k1l2"),
            "azure storage key": secret("AccountKey=", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6A7b8C9d0E1f2G3h4I5j6=="),
            "npm token": secret("npm_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"),
            "sendgrid key": secret("SG.", "A1b2C3d4E5f6G7h8I9j0K1", ".", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v"),
            "aws secret assignment": secret("AWS_SECRET_ACCESS", "_KEY=", "wJalrXUtnFEMIK7MDENGbPxRfiCYQ7XvT2nLw9sA"),
        }
        for name, text in shapes.items():
            with self.subTest(shape=name):
                self.assertTrue(san.leftovers(f"note: {text} end"), f"{name} was not found")

    def test_text_that_only_talks_about_secrets_is_not_taken_for_one(self):
        san = self.san_for()
        for text in (
            "the password is read from process.env.DB_PASSWORD",
            "set AWS_SECRET_ACCESS_KEY=$AWS_SECRET in the shell",
            "a private key block starts with BEGIN PRIVATE KEY lines",
            "see postgres://user@db.internal:5432/app (no password)",
            "the stripe key is sk_live_ followed by letters",
            "token: abc",
            "postgres://user:password@localhost:5432/app is the example in the docs",
            "use postgres://user:${DB_PASSWORD}@db.internal:5432/app",
            "https://user:<token>@example.org/repo.git",
            "redis://default:xxxxxxxx@cache:6379",
            "mysql://root:changeme@localhost/test",
        ):
            with self.subTest(text=text):
                self.assertEqual(san.leftovers(text), {})

    def test_what_developers_really_write_about_secrets_is_not_refused(self):
        san = self.san_for()
        texts = [
            "postgres://postgres:postgres@localhost:5432/mydb",
            "redis://default:redispass@redis:6379/0",
            "mongodb://root:rootpassword@mongo",
            "mongodb://admin:admin123@localhost",
            "postgres://dev:p4ssw0rd@localhost",
            "mysql://root:rootroot@127.0.0.1",
            "postgres://user:mypassword@localhost",
            "postgres://user:YOUR_PASSWORD@localhost",
            "postgres://user:your-password@db",
            "postgres://user:[password]@localhost",
            "services: db: POSTGRES_PASSWORD=postgres and DATABASE_URL=postgres://postgres:postgres@db:5432/app",
            "uses: docker://node:20-alpine@sha256:" + "a1b2c3d4" * 8,
            "http://localhost:3000?next=a@b.co",
            "STRIPE_SECRET_KEY=" + secret("sk_", "test_") + "x" * 24,
            secret("sk_", "live_") + "X" * 24,
            secret("sk_", "test_", "4eC39HqLyjWDarjtT1zdp7dc"),
            secret("https://hooks.slack.com/services/", "T00000000/B00000000/") + "X" * 24,
            "SENDGRID_API_KEY=" + secret("SG", ".") + "x" * 22 + "." + "x" * 43,
            "_authToken=" + secret("npm_") + "x" * 36,
            secret("AIza") + "x" * 35,
            secret("AccountKey=") + "x" * 44 + "==",
            "postgres://myuser:mypassword123@localhost:5432/mydb",
            "postgres://user:YourPassword123@localhost/db",
            "mongodb://admin:password1234@localhost:27017",
            "mongodb://testuser:testpass123456@localhost:27017/testdb",
            "ENV DATABASE_URL=postgresql://app:app_password_2024@db:5432/app",
            "mysql://root:Password2024xyz@localhost",
            "uses: docker://node:20.11.1-alpine3.19@sha256:" + "a1b2c3d4" * 8,
            "image: docker://python:3.12.2-slim-bookworm@sha256:" + "0f9e8d7c" * 8,
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AKIAIOSFODNN7EXAMPLE",
            "The key looks like " + secret("sk_", "live_") + "YourSecretKeyGoesHere1234 and lives in the vault",
            "class AIzaConfigurationManagerFactoryBuilderHelperImpl {}",
            "namespace SG.CustomerManagement.InvoiceProcessing;",
            "Set JWT_PRIVATE_KEY with the full PEM including the " + secret("-----BEGIN ", "PRIVATE KEY-----") + " header and \\n line breaks",
        ]
        for text in texts:
            with self.subTest(text=text[:60]):
                self.assertEqual(san.leftovers(text), {})

    def test_the_lengths_that_decide_between_a_secret_and_a_word_are_pinned(self):
        san = self.san_for()
        self.assertEqual(san.leftovers(secret("mysql://", "svc:", "Xk9fQ2mL8vN", "@db")), {})  # 11 characters
        self.assertTrue(san.leftovers(secret("mysql://", "svc:", "Xk9fQ2mL8vN3", "@db")))  # 12 characters
        self.assertEqual(san.leftovers(secret("AIza") + "SyA1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6qX"), {})  # 36 after the prefix: an identifier
        self.assertTrue(san.leftovers(secret("AIza") + "SyA1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q"))  # 35 after the prefix: a key
        self.assertEqual(san.leftovers(secret("sk_", "live_") + "a1B2c3D4e5F6g7H"), {})  # 15
        self.assertTrue(san.leftovers(secret("sk_", "live_") + "a1B2c3D4e5F6g7H8"))  # 16

    def test_a_password_that_reads_like_a_word_or_holds_an_example_word_is_an_example(self):
        san = self.san_for()
        for password in ("correcthorsebattery1", "summer2024vacation", "iloveyou12345678", "super_secret_value1"):
            with self.subTest(password=password):
                self.assertEqual(san.leftovers(secret("postgres://", "svc:", password, "@db:5432/app")), {})
        for password in ("Xk9fQ2mL8vN3pR7s", "q7zkr2vlm9xat4pw1", "A1b2C3d4E5f6G7h8"):
            with self.subTest(password=password):
                self.assertTrue(san.leftovers(secret("postgres://", "svc:", password, "@db:5432/app")))

    def test_a_password_with_a_colon_is_judged_whole_not_by_what_follows_its_last_colon(self):
        san = self.san_for()
        for password in ("Xk9fQ2mL8vN3pR7s:hello", "Xk9fQ2mL8vN3pR7s:"):
            with self.subTest(password=password):
                self.assertTrue(san.leftovers(secret("postgres://", "svc:", password, "@db:5432/app")))
        # and an example stays an example when the part after a colon is the word
        self.assertEqual(san.leftovers(secret("postgres://", "svc:", "summer2024vacation:holiday", "@db:5432/app")), {})

    def test_the_addresses_that_look_harmless_only_when_the_whole_domain_is(self):
        san = self.san_for(personal_email_patterns=[])

        text, _, _ = san.scrub_text("git@github.com.evil.io:x/y and ana@example.com.evil.io and git@github.com:x/y and ana@example.com")

        self.assertEqual(text, "user@example.com:x/y and user@example.com and git@github.com:x/y and ana@example.com")

    def test_an_account_and_a_trigger_word_a_hundred_characters_apart_still_drop_the_line(self):
        san = self.san_for()

        text, dropped, _ = san.scrub_text("devaccount " + "x" * 100 + " push\nkept")

        self.assertEqual((text, dropped), ("kept", 1))

    def test_the_address_masking_gives_the_same_result_as_the_regular_expression_it_replaced(self):
        import random
        import re

        san = self.san_for(personal_email_patterns=[])
        old = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
        masked = lambda found: found.group(0) if san.HARMLESS_EMAIL.search(found.group(0)) else "user@example.com"
        rng, alphabet = random.Random(20261023), ["a", "B", "1", ".", "_", "%", "+", "-", "@", "@", " ", ":", "/", "git", "example", ".com", ".org", "\n"]
        for _ in range(6000):
            sample = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
            self.assertEqual(san.mask_emails(sample), old.sub(masked, sample), repr(sample))

    def test_a_long_run_of_address_characters_without_an_at_sign_is_not_slow(self):
        san = self.san_for()
        for run in ("a" * 300000, "a-" * 150000, "a.b+c" * 60000, "AIza" * 80000, "sk_live_" * 40000, "SG." * 100000, "http://a:" * 40000):
            with self.subTest(run=run[:12]):
                started = time.monotonic()
                san.scrub_text(run)
                san.leftovers(run)
                self.assertLess(time.monotonic() - started, 6)

    def test_the_refusal_says_where_it_found_something_and_never_what(self):
        home = self.root / "where-home"
        (home / ".config" / "engram-memory-tools").mkdir(parents=True)
        (home / ".config" / "engram-memory-tools" / "patterns.json").write_text(json.dumps(PATTERNS))
        repo = self.root / "where-memory"
        (repo / ".engram").mkdir(parents=True)
        leaked = secret("sk_", "live_", "a1B2c3D4e5F6g7H8i9J0k1L2")
        write_chunk(repo, observation(f"the key is {leaked} today"))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "LC_", "LANG"))}
        env["HOME"] = env["USERPROFILE"] = str(home)

        done = subprocess.run([sys.executable, str(SANITIZER), str(repo)], env=env, capture_output=True, text=True)

        self.assertEqual(done.returncode, 1)
        self.assertIn("stripe key", done.stdout)
        self.assertIn("observation 1, field content", done.stdout)
        self.assertNotIn(leaked, done.stdout + done.stderr)

    def test_a_refusal_that_only_the_text_of_the_chunk_shows_still_says_so(self):
        home = self.root / "text-only-home"
        (home / ".config" / "engram-memory-tools").mkdir(parents=True)
        (home / ".config" / "engram-memory-tools" / "patterns.json").write_text(json.dumps(PATTERNS))
        repo = self.root / "text-only-memory"
        (repo / ".engram").mkdir(parents=True)
        leaked = secret("sk_", "live_", "a1B2c3D4e5F6g7H8i9J0k1L2")
        item = observation("nothing in the values")
        item[leaked] = "the secret is the name of a field"
        write_chunk(repo, item)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "LC_", "LANG"))}
        env["HOME"] = env["USERPROFILE"] = str(home)

        done = subprocess.run([sys.executable, str(SANITIZER), str(repo)], env=env, capture_output=True, text=True)

        self.assertEqual(done.returncode, 1)
        self.assertIn("stripe key", done.stdout)
        self.assertIn("where: no single value holds it; it shows only in the text of the chunk as a whole", done.stdout)
        self.assertNotIn(leaked, done.stdout + done.stderr)

    # --- shapes the privacy file may have ---------------------------------------------------------------------

    def run_with_patterns(self, raw: bytes, chunk_bytes: bytes = None):
        home = self.root / "run-home"
        (home / ".config" / "engram-memory-tools").mkdir(parents=True, exist_ok=True)
        (home / ".config" / "engram-memory-tools" / "patterns.json").write_bytes(raw)
        repo = self.root / "memory"
        (repo / ".engram").mkdir(parents=True, exist_ok=True)
        write_chunk(repo, observation("plain knowledge"))
        if chunk_bytes is not None:
            for path in (repo / ".engram" / "chunks").glob("*.jsonl.gz"):
                path.write_bytes(gzip.compress(chunk_bytes))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "LC_", "LANG"))}
        env["HOME"] = env["USERPROFILE"] = str(home)
        before = {p.name: p.read_bytes() for p in (repo / ".engram").rglob("*") if p.is_file()}
        done = subprocess.run([sys.executable, str(SANITIZER), str(repo)], env=env, capture_output=True)
        after = {p.name: p.read_bytes() for p in (repo / ".engram").rglob("*") if p.is_file()}
        return done, before, after

    def test_a_privacy_file_of_the_wrong_shape_is_refused_with_a_message_and_nothing_changes(self):
        incomplete = {k: v for k, v in PATTERNS.items() if k != "person_name"}
        wrong_type = {**PATTERNS, "account": ["not", "a", "string"]}
        joined = {**PATTERNS, "personal_email_patterns": ["(?P<same>a)", "(?P<same>b)"]}  # each compiles alone, not joined
        for name, raw in (("a list", b"[]"), ("regular expressions that cannot be joined", json.dumps(joined).encode()), ("a missing key", json.dumps(incomplete).encode()), ("a key of the wrong type", json.dumps(wrong_type).encode()), ("a bad regular expression", json.dumps({**PATTERNS, "personal_email_patterns": ["(unclosed"]}).encode())):
            with self.subTest(shape=name):
                done, before, after = self.run_with_patterns(raw)

                stderr = done.stderr.decode("utf-8", "replace")
                self.assertNotEqual(done.returncode, 0)
                self.assertNotIn("Traceback", stderr)
                self.assertIn("patterns.json", stderr)
                self.assertEqual(after, before)

    def test_a_chunk_saved_with_a_byte_order_mark_is_read(self):
        plain = json.dumps({"sessions": [], "observations": [{"id": 1, "sync_id": "obs-1", "type": "decision", "title": "t", "content": "plain knowledge", "session_id": "s"}], "prompts": [], "mutations": []}, ensure_ascii=False, separators=(",", ":"))

        done, _, _ = self.run_with_patterns(json.dumps(PATTERNS).encode(), chunk_bytes=b"\xef\xbb\xbf" + plain.encode("utf-8"))

        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"Traceback", done.stderr)

    # --- names written with letters that have no decomposition, and with other separators ------------------------

    def test_letters_without_a_decomposition_match_their_plain_form_in_both_directions(self):
        for written, typed in (("Łukasz Wiśniewski", "Lukasz Wisniewski"), ("Lukasz Wisniewski", "Łukasz Wiśniewski"), ("Søren Ørsted", "Soren Orsted"), ("Soren Orsted", "SØREN ØRSTED"), ("Đorđe Đurić", "Dorde Duric"), ("Ægir Þórsson", "Aegir Thorsson"), ("Œdipe Roi", "Oedipe Roi")):
            with self.subTest(file=written, text=typed):
                san = self.san_for(person_name=written)

                self.assertEqual(san.scrub_text(f"by {typed} today")[0], "by the author today")

    def test_a_name_joined_by_another_separator_is_still_the_name(self):
        san = self.san_for(person_name="Ana María Núñez")
        for text in ("Ana_María_Núñez", "ana.maria.nunez", "Ana-María-Núñez", "AnaMaríaNúñez", "Ana  María_Núñez", "ana_maria nunez"):
            with self.subTest(text=text):
                self.assertEqual(san.scrub_text(f"by {text} today")[0], "by the author today")

    def test_a_name_that_has_a_dash_or_a_dot_of_its_own_is_found_with_any_separator_too(self):
        san = self.san_for(person_name="Ana Núñez-Ortiz")
        for text in ("Ana Núñez-Ortiz", "Ana Nunez Ortiz", "ana_nunez_ortiz", "AnaNunezOrtiz", "ana.nunez.ortiz", "Ana  Núñez - Ortiz"):
            with self.subTest(text=text):
                self.assertEqual(san.scrub_text(f"by {text} today")[0], "by the author today")
        dotted = self.san_for(person_name="J.P. Ortiz")
        self.assertEqual(dotted.scrub_text("by JP Ortiz and J P Ortiz today")[0], "by the author and the author today")

    def test_a_name_made_only_of_separators_matches_nothing(self):
        san = self.san_for(person_name="- . _")

        self.assertEqual(san.scrub_text("a - b . c _ d")[0], "a - b . c _ d")

    def test_a_separator_does_not_make_other_words_the_name(self):
        san = self.san_for(person_name="Ana María")

        for text in ("Anaconda María", "Ana Mariano", "Banana María"):
            with self.subTest(text=text):
                self.assertEqual(san.scrub_text(f"see {text} here")[0], f"see {text} here")


class MergeManifestByteOrderMarkTest(unittest.TestCase):
    def test_a_manifest_side_with_a_byte_order_mark_is_merged(self):
        with tempfile.TemporaryDirectory(prefix="hardening ") as tmp:
            repo = Path(tmp) / "memory"
            (repo / ".engram").mkdir(parents=True)
            env = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com", "HOME": tmp, "USERPROFILE": tmp}
            env = {k: v for k, v in env.items() if not k.startswith("GIT_DIR")}

            def git(*args):
                return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True)

            def save(ids, bom=False):
                manifest = {"version": 2, "chunks": [{"id": i, "created_by": "owner", "created_at": "2026-01-01T00:00:00Z", "sessions": 0, "memories": 1, "prompts": 0} for i in ids]}
                (repo / ".engram" / "manifest.json").write_bytes((b"\xef\xbb\xbf" if bom else b"") + (json.dumps(manifest, indent=2) + "\n").encode())

            git("init", "-q", "-b", "main")
            save(["aaaaaaaa"]); git("add", "-A"); git("commit", "-q", "-m", "first")
            git("switch", "-q", "-c", "other")
            save(["aaaaaaaa", "bbbbbbbb"], bom=True); git("add", "-A"); git("commit", "-q", "-m", "other")
            git("switch", "-q", "main")
            save(["aaaaaaaa", "cccccccc"]); git("add", "-A"); git("commit", "-q", "-m", "this")
            self.assertNotEqual(git("merge", "-q", "other").returncode, 0)

            done = subprocess.run([sys.executable, str(MERGE_MANIFEST), str(repo)], env=env, capture_output=True)

            self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
            merged = json.loads((repo / ".engram" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual({c["id"] for c in merged["chunks"]}, {"aaaaaaaa", "bbbbbbbb", "cccccccc"})


if __name__ == "__main__":
    unittest.main()
