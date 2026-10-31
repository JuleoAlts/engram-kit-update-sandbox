#!/usr/bin/env python3
"""Resolve a git conflict in .engram/manifest.json by keeping the chunks of BOTH sides.

Usage: python3 tools/merge_manifest.py <memory-repo-dir>      (run while a rebase or a merge is stopped)

The manifest is only an index of chunk ids: every chunk is its own file named by its content, so two
developers who share memory at about the same time never conflict on a chunk, only on the index (both
append a line at the end of the same list). Taking the union of both lists, without repeating an id, is
always correct. The order is: the chunks of the stage that was already there first, then the new ones.
"""
import json
import subprocess
import sys
from pathlib import Path

EMPTY = {"version": 2, "chunks": []}


def read_stage(repo: Path, stage: int) -> dict:
    """The manifest as one side of the conflict has it (2 = the branch being rebased onto, 3 = the commit being replayed)."""
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f":{stage}:.engram/manifest.json"],
        capture_output=True,
    )
    # Decoded here, not by subprocess: on Windows it decodes in a thread of its own, where the error cannot be caught. First
    # of all, as subprocess did: what counts as blank is decided on the text, and an output that is not UTF-8 is an error
    # whatever the status of git.
    text = result.stdout.decode("utf-8")
    if result.returncode != 0 or not text.strip():
        return dict(EMPTY, chunks=[])
    return json.loads(text.lstrip("\ufeff"))  # an editor on Windows may have added a byte order mark


def union(first: dict, second: dict) -> dict:
    seen, chunks = set(), []
    for entry in list(first.get("chunks", [])) + list(second.get("chunks", [])):
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        chunks.append(entry)
    return {"version": max(first.get("version", 1), second.get("version", 1)), "chunks": chunks}


def main(repo: Path) -> int:
    try:
        merged = union(read_stage(repo, 2), read_stage(repo, 3))
    except UnicodeDecodeError as exc:
        print(f"a manifest in the conflict is not UTF-8 text ({exc.reason} at byte {exc.start}): nothing was merged, "
              "solve the conflict by hand.", file=sys.stderr)
        return 1
    # As bytes: text mode would write CRLF on Windows, and the sync would then see the manifest as modified.
    (repo / ".engram" / "manifest.json").write_bytes((json.dumps(merged, indent=2) + "\n").encode("utf-8"))
    print(f"manifest merged: {len(merged['chunks'])} chunks")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(Path(sys.argv[1]).resolve()))
# stale line: this copy is behind the kit
