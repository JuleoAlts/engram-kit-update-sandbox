"""Tests of tools/merge_manifest.py: the union of the chunk lists of two developers.

Run them from the repository root with:  python3 -m unittest discover -s tests
"""
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "merge_manifest.py"
spec = importlib.util.spec_from_file_location("merge_manifest_under_test", SCRIPT)
merge_manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge_manifest)


def chunk(chunk_id: str) -> dict:
    return {"id": chunk_id, "created_by": "owner", "created_at": "2026-01-01T00:00:00Z", "sessions": 1, "memories": 1, "prompts": 0}


class UnionTest(unittest.TestCase):
    def test_both_sides_keep_their_chunks(self):
        merged = merge_manifest.union({"version": 2, "chunks": [chunk("aaaaaaaa"), chunk("bbbbbbbb")]}, {"version": 2, "chunks": [chunk("aaaaaaaa"), chunk("cccccccc")]})

        self.assertEqual([c["id"] for c in merged["chunks"]], ["aaaaaaaa", "bbbbbbbb", "cccccccc"])

    def test_a_chunk_on_both_sides_is_not_repeated(self):
        merged = merge_manifest.union({"version": 2, "chunks": [chunk("aaaaaaaa")]}, {"version": 2, "chunks": [chunk("aaaaaaaa")]})

        self.assertEqual(len(merged["chunks"]), 1)

    def test_the_order_is_the_existing_ones_first_and_the_new_ones_after(self):
        merged = merge_manifest.union({"version": 2, "chunks": [chunk("1")]}, {"version": 2, "chunks": [chunk("1"), chunk("2"), chunk("3")]})

        self.assertEqual([c["id"] for c in merged["chunks"]], ["1", "2", "3"])

    def test_an_empty_side_changes_nothing(self):
        merged = merge_manifest.union({"version": 2, "chunks": []}, {"version": 2, "chunks": [chunk("aaaaaaaa")]})

        self.assertEqual([c["id"] for c in merged["chunks"]], ["aaaaaaaa"])

    def test_the_higher_version_wins(self):
        merged = merge_manifest.union({"version": 1, "chunks": []}, {"version": 2, "chunks": []})

        self.assertEqual(merged["version"], 2)

    def test_a_side_without_a_version_still_works(self):
        merged = merge_manifest.union({"chunks": [chunk("a")]}, {"chunks": [chunk("b")]})

        self.assertEqual([c["id"] for c in merged["chunks"]], ["a", "b"])
        self.assertEqual(merged["version"], 1)


if __name__ == "__main__":
    unittest.main()
