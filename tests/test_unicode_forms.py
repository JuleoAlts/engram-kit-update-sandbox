"""Tests that the sanitizer finds a name however it was typed, and that it really verifies the manifest.

The same name can reach the shared memory in many forms that look identical to a person: with the accent as one
character or as a letter plus a combining mark (macOS text is often the second), with a non-breaking space or two
spaces or a line break between the words, with an invisible character inside, with fullwidth letters, with a
non-breaking hyphen, in capitals ("STRAUSS" for "Strauß"), or without the accents. A pattern that only matches the
exact characters of the privacy file lets all of them through, and the final check, which uses the same pattern,
cannot see them either.

Run them from the repository root with:  python3 -m unittest discover -s tests
Every test uses its own temporary HOME with a fake privacy file.
"""
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
import unicodedata
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "sanitize_chunks.py"

PATTERNS = {
    "personal_email_patterns": [r"dev@example\.com"],
    "other_identity_patterns": [r"otheridentity"],
    "account": "devaccount",
    "person_name": "Dev Person",
    "username": "devuser",
    "home_path": "/Users/devuser",
}

NFC = lambda text: unicodedata.normalize("NFC", text)  # noqa: E731
NFD = lambda text: unicodedata.normalize("NFD", text)  # noqa: E731


def load_sanitizer(home: str, patterns: dict):
    """Import the script with HOME (and USERPROFILE, which Windows reads) pointing to a folder that holds `patterns`."""
    config = Path(home, ".config", "engram-memory-tools")
    config.mkdir(parents=True, exist_ok=True)
    (config / "patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    previous = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = home
    try:
        spec = importlib.util.spec_from_file_location("sanitize_chunks_forms_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def write_chunk(repo: Path, data: dict, manifest_extra: dict = None, **entry_changes) -> str:
    plain = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    chunk_id = hashlib.sha256(plain.encode("utf-8")).hexdigest()[:8]
    (repo / ".engram" / "chunks").mkdir(parents=True, exist_ok=True)
    (repo / ".engram" / "chunks" / f"{chunk_id}.jsonl.gz").write_bytes(gzip.compress(plain.encode("utf-8")))
    entry = {"id": chunk_id, "created_by": "someone", "created_at": "2026-01-01T00:00:00Z", "sessions": 0, "memories": 1, "prompts": 0}
    entry.update(entry_changes)
    manifest = {"version": 2, "chunks": [entry]}
    manifest.update(manifest_extra or {})
    (repo / ".engram" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return chunk_id


def observation(content: str) -> dict:
    return {"sessions": [], "observations": [{"id": 1, "sync_id": "obs-1", "type": "decision", "title": "notes", "content": content, "session_id": "s1"}], "prompts": [], "mutations": []}


class FoldedMatchingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="forms ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.n = 0

    def san_for(self, **changes):
        self.n += 1
        return load_sanitizer(str(self.root / f"home{self.n}"), {**PATTERNS, **changes})

    def scrub(self, san, text):
        return san.scrub_text(text)[0]

    def test_the_same_accented_name_is_masked_composed_or_decomposed_whichever_the_privacy_file_has(self):
        for pattern_form, text_form in ((NFC, NFD), (NFD, NFC)):
            with self.subTest(pattern=pattern_form.__name__, text=text_form.__name__):
                san = self.san_for(person_name=pattern_form("María Núñez"))

                self.assertEqual(self.scrub(san, f"Notes by {text_form('María Núñez')} today"), "Notes by the author today")

    def test_any_kind_of_space_or_a_line_break_between_the_words_of_a_name_is_still_the_name(self):
        san = self.san_for(person_name="María Núñez")
        for gap in ("\u00a0", "  ", "\n", "\r\n", "\t", "\u2009", "\u3000"):
            with self.subTest(gap=repr(gap)):
                self.assertEqual(self.scrub(san, f"by María{gap}Núñez."), "by the author.")

    def test_invisible_characters_inside_a_name_do_not_hide_it_and_are_replaced_with_it(self):
        san = self.san_for(person_name="María Núñez")
        for invisible in ("\u00ad", "\u200b", "\u200d", "\ufeff"):
            with self.subTest(invisible=repr(invisible)):
                self.assertEqual(self.scrub(san, f"by Ma{invisible}ría Nú{invisible}ñez."), "by the author.")

    def test_fullwidth_letters_a_non_breaking_hyphen_and_capitals_are_the_same_name(self):
        san = self.san_for(person_name="Ana-María Strauß")
        for text in ("Ana\u2011María Strauß", "Ana\u2010María Strauß", "ＡＮＡ-ＭＡＲＩ\u0301Ａ STRAUSS", "ana-maría strauss"):
            with self.subTest(text=text):
                self.assertEqual(self.scrub(san, f"by {text}."), "by the author.")

    def test_a_name_typed_without_its_accents_is_masked_too(self):
        # The same person writes "Maria Nunez" where the keyboard or the tool had no accents.
        san = self.san_for(person_name="María Núñez")

        self.assertEqual(self.scrub(san, "by Maria Nunez and MARIA NUNEZ"), "by the author and the author")

    def test_a_username_and_a_windows_home_folder_are_found_in_any_form(self):
        san = self.san_for(username="josé", home_path="C:\\Users\\José")

        text = self.scrub(san, f"user {NFD('José')} opened C:\\Users\\{NFD('José')}\\proj and C:\\Users\\JOSE\\x")

        self.assertEqual(text, "user user opened ~\\proj and ~\\x")

    def test_the_account_is_found_in_any_form(self):
        san = self.san_for(account="josé-dev")

        self.assertEqual(self.scrub(san, f"owned by {NFD('José')}\u2011Dev here"), "owned by <owner-account> here")

    def test_other_words_are_not_taken_for_the_name(self):
        san = self.san_for(person_name="María Núñez", username="ana")

        for text in ("Mariana Nunez", "María Núñezes", "banana", "analysis"):
            with self.subTest(text=text):
                self.assertEqual(self.scrub(san, f"see {text} now"), f"see {text} now")
        self.assertEqual(self.scrub(san, "see Ana's note"), "see user's note")  # the username `ana`, as a whole word

    def test_only_the_characters_of_the_name_are_replaced_and_the_rest_of_the_text_is_untouched(self):
        san = self.san_for(person_name="María Núñez")

        text = self.scrub(san, "ñandú\u200b María\u00a0Núñez\u200b \u00e9\u0301 end")

        self.assertEqual(text, "ñandú\u200b the author\u200b \u00e9\u0301 end")

    def test_the_leftover_check_sees_the_same_forms_as_the_cleaning(self):
        san = self.san_for(person_name="María Núñez")
        dirty = f"by {NFD('María')}\u00a0Núñez"

        self.assertIn("person name", san.leftovers(dirty))
        self.assertEqual(san.leftovers(self.scrub(san, dirty)), {})

    def test_the_check_counts_each_occurrence_once(self):
        san = self.san_for()
        data = {"topic_key": "by Dev Person", "title": "Dev Person again"}

        self.assertEqual(san.leftovers_in(data, json.dumps(data)), {"person name": 2})
        self.assertEqual(san.leftovers_in({"a": "plain"}, '{"a": "plain"}'), {})

    def test_a_value_that_is_only_invisible_characters_matches_nothing(self):
        san = self.san_for(person_name="\u200b \u00ad", username="\ufeff")

        text = "any text; with  gaps, (and) - dashes ... and  \u00ad\u200b  more"
        self.assertEqual(self.scrub(san, text), text)
        self.assertEqual(san.leftovers(text), {})

    def test_an_invisible_character_glued_to_a_name_does_not_hide_it(self):
        # Pasted rich text carries zero-width characters next to words: they are not part of the word.
        san = self.san_for()
        for invisible in ("\u200b", "\ufeff", "\u2060", "\u200e", "\u00ad"):
            with self.subTest(invisible=repr(invisible)):
                self.assertEqual(self.scrub(san, f"written by{invisible}Dev Person today"), "written by" + invisible + "the author today")
                self.assertEqual(self.scrub(san, f"Dev Person{invisible}wrote this"), "the author" + invisible + "wrote this")
                self.assertEqual(self.scrub(san, f"by{invisible}devuser"), "by" + invisible + "user")

    def test_a_name_right_after_a_letter_with_a_combining_mark_is_still_inside_a_word(self):
        san = self.san_for()

        self.assertEqual(self.scrub(san, "see e\u0301Dev Person and Dev Persone\u0301 here"), "see e\u0301Dev Person and Dev Persone\u0301 here")

    def test_the_dotless_i_is_an_i(self):
        san = self.san_for(username="ismail")

        self.assertEqual(self.scrub(san, "hi ısmail and İSMAİL and Ismail here"), "hi user and user and user here")

    def test_a_character_that_folds_to_two_is_replaced_once(self):
        san = self.san_for(username="s")

        self.assertEqual(self.scrub(san, "x ß y"), "x user y")

    def test_the_folder_check_uses_the_original_characters_around_it(self):
        san = self.san_for(home_path="/root")

        text = self.scrub(san, "in /root\u200b and /root\u2011x and /root-x and /root.x and /root. and ./root and a./root")

        # Only an ASCII hyphen makes "/root-x" another folder; a non-breaking one is masked, as it always was.
        self.assertEqual(text, "in ~\u200b and ~\u2011x and /root-x and /root.x and ~. and ./root and a./root")

    def test_plain_ascii_text_is_cleaned_as_before(self):
        san = self.san_for()

        text = self.scrub(san, "Dev Person (devuser) works in /Users/devuser/app for devaccount; dev@example.com")

        self.assertEqual(text, "the author (user) works in ~/app for <owner-account>; user@example.com")


class UncleanedFieldsTest(unittest.TestCase):
    """The cleaning rewrites the title, the content and the directory; every other text is only CHECKED, and a name
    found there stops everything. The check reads the decoded strings, so a line break inside a name is seen."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="forms ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "memory"
        (self.repo / ".engram").mkdir(parents=True)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in (self.repo / ".engram").rglob("*") if p.is_file()}

    def run_with_topic(self, topic_key, **patterns):
        san = load_sanitizer(str(self.root / "home"), {**PATTERNS, **patterns})
        data = observation("plain knowledge")
        data["observations"][0]["topic_key"] = topic_key
        write_chunk(self.repo, data)
        before = self.snapshot()
        return san.main(self.repo), before

    def test_a_name_split_by_a_line_break_in_a_field_that_is_not_cleaned_stops_everything(self):
        for gap in ("\n", "\r\n", "\t", "\f", "\u2028", "\u00a0"):
            with self.subTest(gap=repr(gap)):
                self.setUp()
                code, before = self.run_with_topic(f"Ana-María{gap}Núñez", person_name="Ana-María Núñez")

                self.assertEqual(code, 1)
                self.assertEqual(self.snapshot(), before)

    def test_a_name_with_an_invisible_character_in_a_field_that_is_not_cleaned_stops_everything(self):
        code, before = self.run_with_topic("by\u200bDev Person")

        self.assertEqual(code, 1)
        self.assertEqual(self.snapshot(), before)

    def test_a_field_without_the_name_does_not_stop_anything(self):
        code, _ = self.run_with_topic("tour/itinerary")

        self.assertEqual(code, 0)


class ManifestVerificationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="forms ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "memory"
        (self.repo / ".engram").mkdir(parents=True)

    def san_for(self, **changes):
        return load_sanitizer(str(self.root / "home"), {**PATTERNS, **changes})

    def manifest(self):
        return json.loads((self.repo / ".engram" / "manifest.json").read_text(encoding="utf-8"))

    def snapshot(self):
        return {p.name: p.read_bytes() for p in (self.repo / ".engram").rglob("*") if p.is_file()}

    def test_the_name_of_the_person_does_not_stay_in_the_manifest_even_in_ascii(self):
        san = self.san_for()
        write_chunk(self.repo, observation("plain knowledge"), created_by="Dev Person")

        self.assertEqual(san.main(self.repo), 0)

        self.assertEqual(self.manifest()["chunks"][0]["created_by"], "owner")

    def test_an_accented_username_in_the_manifest_is_found_whatever_its_form(self):
        san = self.san_for(username="josé", person_name="Someone Else")
        write_chunk(self.repo, observation("plain knowledge"), created_by=NFD("josé"))

        self.assertEqual(san.main(self.repo), 0)

        self.assertEqual(self.manifest()["chunks"][0]["created_by"], "owner")

    def test_a_username_that_json_would_write_as_an_escape_is_found_in_the_manifest(self):
        # json.dumps writes "Ł" as \\u0142: checking that text would never see the name.
        san = self.san_for(username="Łukasz", person_name="Someone Else")
        write_chunk(self.repo, observation("plain knowledge"), created_by="Łukasz")

        self.assertEqual(san.main(self.repo), 0)

        self.assertEqual(self.manifest()["chunks"][0]["created_by"], "owner")

    def test_only_the_entries_that_have_something_lose_their_attribution(self):
        san = self.san_for()
        first = write_chunk(self.repo, observation("plain knowledge"), created_by="Dev Person")
        second_data = observation("another plain knowledge")
        plain = json.dumps(second_data, ensure_ascii=False, separators=(",", ":"))
        second = hashlib.sha256(plain.encode("utf-8")).hexdigest()[:8]
        (self.repo / ".engram" / "chunks" / f"{second}.jsonl.gz").write_bytes(gzip.compress(plain.encode("utf-8")))
        manifest = self.manifest()
        manifest["chunks"].append({"id": second, "created_by": "Julian", "created_at": "2026-01-02T00:00:00Z", "sessions": 0, "memories": 1, "prompts": 0})
        (self.repo / ".engram" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        self.assertEqual(san.main(self.repo), 0)

        authors = {entry["id"]: entry["created_by"] for entry in self.manifest()["chunks"]}
        self.assertEqual(authors, {first: "owner", second: "Julian"})

    def test_the_folding_cache_does_not_keep_the_texts_after_the_run(self):
        san = self.san_for()
        write_chunk(self.repo, observation("Notes by Dev Person"))

        self.assertEqual(san.main(self.repo), 0)

        self.assertEqual(san._fold.cache_info().currsize, 0)

    def test_a_name_split_by_a_line_break_in_the_manifest_is_found(self):
        san = self.san_for(person_name="Ana-María Núñez")
        write_chunk(self.repo, observation("plain knowledge"), created_by="Ana-María\nNúñez")

        self.assertEqual(san.main(self.repo), 0)

        self.assertEqual(self.manifest()["chunks"][0]["created_by"], "owner")

    def test_a_name_in_an_unexpected_top_level_field_of_the_manifest_stops_everything(self):
        san = self.san_for()
        write_chunk(self.repo, observation("plain knowledge"), manifest_extra={"note": "kept by Dev Person"}, created_by="owner")
        before = self.snapshot()

        self.assertEqual(san.main(self.repo), 1)

        self.assertEqual(self.snapshot(), before)

    def test_a_username_equal_to_the_vocabulary_of_the_manifest_does_not_trap_the_cleaning(self):
        # `owner` is what the blanking writes and `version` and `chunks` are its own fields: none of them is personal data.
        for username in ("owner", "version", "chunks", "created_by"):
            with self.subTest(username=username):
                shutil.rmtree(self.repo / ".engram")
                (self.repo / ".engram").mkdir()
                san = self.san_for(username=username, person_name="Someone Else")
                write_chunk(self.repo, observation("plain knowledge"), created_by="owner")
                before = self.snapshot()

                self.assertEqual(san.main(self.repo), 0)

                self.assertEqual(self.snapshot(), before)

    def test_the_account_in_the_manifest_is_still_cleaned(self):
        san = self.san_for()
        write_chunk(self.repo, observation("plain knowledge"), created_by="devaccount")

        self.assertEqual(san.main(self.repo), 0)

        self.assertEqual(self.manifest()["chunks"][0]["created_by"], "owner")

    def test_what_is_left_in_the_manifest_after_cleaning_stops_everything_and_nothing_is_written(self):
        # Only `created_by` can be blanked; the same data in another field cannot, so the cleaning must refuse.
        san = self.san_for()
        write_chunk(self.repo, observation("Notes by Dev Person"), created_by="Dev Person", note="written by Dev Person")
        before = self.snapshot()

        self.assertEqual(san.main(self.repo), 1)

        self.assertEqual(self.snapshot(), before)

    def test_a_manifest_with_nothing_personal_is_left_byte_for_byte(self):
        san = self.san_for()
        write_chunk(self.repo, observation("plain knowledge"), created_by="owner")
        before = self.snapshot()

        self.assertEqual(san.main(self.repo), 0)

        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
