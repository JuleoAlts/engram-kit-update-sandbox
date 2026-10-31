#!/usr/bin/env python3
"""Make Engram Git Sync chunks safe and generic for a team: no prompts, no personal data.

Usage: python3 tools/sanitize_chunks.py <memory-repo-dir> [--only-new]   (it lives in the memory repository, in tools/)
--only-new leaves the chunks that are already committed as they are (the sync script always passes it).

The memory repo keeps its chunks in `<dir>/.engram/chunks/<id>.jsonl.gz` and lists them in
`<dir>/.engram/manifest.json`. A chunk id is the first 8 hex characters of the SHA-256 of the plain
(uncompressed) JSON, so any change gives the chunk a NEW id; the previous file is moved to
`~/.engram/local-only/` and never stays in the repo.

What the cleaned chunk keeps: the project knowledge (architecture, decisions, designs, bug fixes,
discoveries, rules of the product and of the code).
What it removes or replaces:
  * all user prompts;
  * every line about one person's own accounts, credentials, git identity or push routine, and every
    line that names another identity;
  * preference observations that name a person or that are about that person's tooling;
  * emails, the person's name, the macOS username and home path, and the GitHub account name
    (replaced by neutral placeholders). A name, a username, an account or a folder is found however it was typed:
    accents composed or decomposed or missing, capitals, any kind of space or dash between the words, invisible
    characters inside, fullwidth letters. Deliberately it masks "Maria" where the privacy file says "María": for
    a guard, masking too much is the safe side;
  * every relation between observations that points to an observation which is not in the SAME chunk (for
    example one that was excluded above), and the free-text reason of the relations that stay (it can describe
    an observation that was excluded).
The script then verifies that NONE of those identifiers is left anywhere in the chunk or the manifest (in the
manifest the only free text, `created_by`, is blanked first) and refuses to write anything if one is. It is idempotent: a clean chunk is left as it is.

The work has two phases so that an interruption never leaves the repository half way. Phase 1 reads, cleans and
verifies EVERY chunk in memory and writes nothing. Phase 2 writes the new chunk files, replaces the manifest in
one atomic step and moves the previous files to `~/.engram/local-only/` last, so the manifest on disk only ever
lists files that exist. If phase 2 is interrupted, running the script again finishes the job.
"""
from __future__ import annotations  # the type hints below must not need Python 3.9 (an older machine has 3.8)

import functools
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from array import array
from pathlib import Path

LOCAL_ONLY = Path.home() / ".engram" / "local-only"

# Also matches the truncated forms (titles are cut at a fixed length, so an address can end mid-way).
CONFIG = Path.home() / ".config" / "engram-memory-tools" / "patterns.json"


def use_utf8_console() -> None:
    """A message may carry a folder name. The console of Windows uses a code page that cannot show every character,
    and a print that fails would stop the cleaning half way, after the new chunks were already written."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def _checked(data) -> dict:
    """The privacy file must be an object with the four strings and, if given, two lists of valid regular expressions."""
    problems = []
    if not isinstance(data, dict):
        sys.exit(f"{CONFIG} must hold a JSON object with account, person_name, username and home_path (a {type(data).__name__} "
                 "was found). Nothing was changed.")
    for key in ("account", "person_name", "username", "home_path"):
        if not isinstance(data.get(key), str):
            problems.append(f"{key} must be a string")
    for key in ("personal_email_patterns", "other_identity_patterns"):
        value = data.get(key)
        if value is None:
            continue
        if not (isinstance(value, list) and all(isinstance(item, str) for item in value)):
            problems.append(f"{key} must be a list of strings")
            continue
        for item in value:
            try:
                re.compile(item)
            except re.error as exc:
                problems.append(f"{key} has a regular expression that does not compile ({exc})")
        if not problems:
            try:
                re.compile("|".join(value or [r"(?!x)x"]), re.I)  # the tool joins them with |
            except re.error as exc:
                problems.append(f"the expressions of {key} cannot be joined into one ({exc}): put any flag such as (?i) in each one only if it is the first, or drop it")
    if problems:
        sys.exit(f"{CONFIG} cannot be used: " + "; ".join(problems) + ". Nothing was changed.")
    return data


def load_identifiers() -> dict:
    """The identifiers to remove live in a LOCAL file that is never committed, so this script stays generic.
    The file is UTF-8 on every machine (a byte order mark is accepted): reading it with the default encoding of
    the machine would turn an accented name into other characters, which would then never be found."""
    if not CONFIG.exists():
        sys.exit(f"Missing {CONFIG}. Create it with: personal_email_patterns, other_identity_patterns (lists of "
                 "regular expressions) and account, person_name, username, home_path (strings). Nothing was changed.")
    try:
        return _checked(json.loads(CONFIG.read_text(encoding="utf-8-sig")))
    except UnicodeDecodeError as exc:
        sys.exit(f"{CONFIG} is not UTF-8 text ({exc.reason} at byte {exc.start}). Save it as UTF-8, or write every "
                 "character that is not ASCII as a \\uXXXX escape. Nothing was changed.")
    except ValueError as exc:
        sys.exit(f"{CONFIG} is not valid JSON ({exc}). Nothing was changed.")


if __name__ == "__main__":  # only when run as a script: whoever imports this module (the tests) keeps its own streams
    use_utf8_console()


_ID = load_identifiers()
NEVER = r"(?!x)x"  # matches nothing: used when a list of identifiers is empty
PERSONAL_EMAIL = re.compile("|".join(_ID.get("personal_email_patterns") or [NEVER]), re.I)
WORK_IDENTITY = re.compile("|".join(_ID.get("other_identity_patterns") or [NEVER]), re.I)


# Letters that Unicode does not decompose into a base letter and a mark, with the form people type without them.
_PLAIN_FORMS = {ord(k): v for k, v in {
    "\u0131": "i", "\u0142": "l", "\u00f8": "o", "\u0111": "d", "\u00f0": "d", "\u0127": "h",
    "\u00e6": "ae", "\u0153": "oe", "\u00fe": "th",
}.items()}


def _fold_cluster(cluster: str) -> str:
    """One letter with its combining marks, in the form that does not depend on how it was typed: without accents
    ("á" and "a" + a combining mark and "a" are the same), without invisible characters (soft hyphen, zero-width
    space), every kind of space as one space, every kind of dash as "-", fullwidth and compatibility forms as their
    plain letters, and in lower case with the full folding ("Strauß" and "STRAUSS"). The dotless "ı" is an "i", as
    it always was for `re.I`."""
    out = []
    for char in unicodedata.normalize("NFKD", cluster):
        category = unicodedata.category(char)
        if category[0] == "M" or category == "Cf":
            continue
        if char.isspace():
            out.append(" ")
        elif category == "Pd":
            out.append("-")
        else:
            out.append(char)
    return "".join(out).casefold().translate(_PLAIN_FORMS)


@functools.lru_cache(maxsize=4)
def _fold(text: str) -> tuple:
    """(folded text, where each folded character starts in `text`, where it ends). A run of spaces is one space."""
    folded, starts, ends = [], array("I"), array("I")
    size, i = len(text), 0
    while i < size:
        j = i + 1
        while j < size and text[j] >= "\u0300" and unicodedata.category(text[j])[0] == "M":
            j += 1
        cluster = text[i:j]
        # Plain ASCII (the almost-always case) only changes its capitals; the rest goes through the full folding.
        piece = cluster.lower() if j == i + 1 and cluster < "\x80" and not cluster.isspace() else _fold_cluster(cluster)
        if piece == " " and folded and folded[-1] == " ":
            ends[-1] = j
        else:
            for char in piece:
                folded.append(char)
                starts.append(i)
                ends.append(j)
        i = j
    return "".join(folded), starts, ends


def _is_word(char: str) -> bool:
    return char.isalnum() or char == "_"


def _word_before(text: str, index: int) -> bool:
    """Is the character before `index` part of a word? Combining marks belong to the letter they follow; an invisible
    character is not part of a word (the original text decides the boundary, not the folded one)."""
    index -= 1
    while index >= 0 and unicodedata.category(text[index])[0] == "M":
        index -= 1
    return index >= 0 and _is_word(text[index])


class Folded:
    """A name, an account or a folder, found however it was typed.

    The text is compared in its folded form (see `_fold_cluster`) and what is replaced is the original characters
    that the match came from, so the rest of the text is untouched. Whether the match is a whole word (or a whole
    path) is decided on the ORIGINAL characters around it: an invisible character glued to a name must not hide it.
    It answers like a compiled regular expression to the four calls this script makes: search, findall, sub and
    the truth of the result. A value that folds to nothing (only invisible characters) matches nothing, instead of
    matching everywhere."""

    def __init__(self, value: str, path: bool = False):
        needle = _fold(value)[0].strip()
        self.path = path
        # The words of a name may be joined by spaces, dots, underscores, dashes or nothing at all ("Dev_Person",
        # "dev.person", "DevPerson"). A folder is matched as it is. The lookahead lets overlapping starts be tried.
        # A dash, dot or underscore that the name has itself is a separator too ("Núñez-Ortiz" is also "Nunez Ortiz").
        words = [re.escape(word) for word in re.split(r"[ _.\-]+", needle) if word] if (needle and not path) else [re.escape(needle)]
        body = r"[ _.\-]*".join(words)
        self.regex = re.compile("(?=(" + body + "))") if needle and words else None

    def _whole(self, text: str, start: int, end: int) -> bool:
        after = text[end] if end < len(text) else ""
        if self.path:
            dash = after == "-"  # only the ASCII hyphen, as before: "/root-x" is another folder, "/root\u2011x" is not
            dot_word = after == "." and end + 1 < len(text) and _is_word(text[end + 1])
            before_dot = start > 0 and text[start - 1] == "."
            return not (_word_before(text, start) or before_dot or (after != "" and _is_word(after)) or dash or dot_word)
        return not (_word_before(text, start) or (after != "" and _is_word(after)))

    def spans(self, text: str) -> list:
        if self.regex is None:
            return []
        folded, starts, ends = _fold(text)
        found, last_end = [], 0
        for match in self.regex.finditer(folded):
            first, last = match.start(1), match.end(1) - 1
            start, end = starts[first], ends[last]
            if start >= last_end and self._whole(text, start, end):
                found.append((start, end))
                last_end = end
        return found

    def search(self, text: str) -> bool:
        return bool(self.spans(text))

    def findall(self, text: str) -> list:
        return [text[start:end] for start, end in self.spans(text)]

    def sub(self, replacement: str, text: str) -> str:
        pieces, position = [], 0
        for start, end in self.spans(text):
            pieces += [text[position:start], replacement]
            position = end
        return "".join(pieces) + text[position:]


def whole_word(value: str) -> Folded:
    """`value` only as a whole word: a username `ana` is not inside `banana`, a name `Ana` is not inside `Banana`."""
    return Folded(value)


def whole_path(value: str) -> Folded:
    """A folder such as /root only as a whole path: not inside app/root.tsx, /root.tsx or /rootfs."""
    return Folded(value, path=True)


ACCOUNT = whole_word(_ID["account"])
NAME = whole_word(_ID["person_name"])
HOME_PATH = whole_path(_ID["home_path"])
USERNAME = whole_word(_ID["username"])
_ACC = re.escape(_ID["account"])
# A machine that cannot know the owner's addresses (a cloud session) still must not share one: any address that
# is not an example, a bot's no-reply or a git remote (git@github.com:...) is masked as well.
# The address regex `[local]+@domain` is quadratic on a long run of local-part characters with no @ (100 KB took 16
# seconds), so the runs are found first (linear) and only a run that ends at an @ is looked at.
LOCAL_RUN = re.compile(r"[A-Za-z0-9._%+-]+")
DOMAIN = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# An address at example.com/org/net, a bot's no-reply, or the `git@` of a well-known host (a remote such as
# git@github.com:team/repo). The `git@` of any other host can name an internal server, so it is masked.
HARMLESS_EMAIL = re.compile(
    r"@example\.(?:com|org|net)$|^noreply@anthropic\.com$"
    r"|^git@(?:github\.com|gitlab\.com|bitbucket\.org|ssh\.dev\.azure\.com|vs-ssh\.visualstudio\.com)$",
    re.I,
)


def mask_emails(text: str) -> str:
    """Every address replaced by user@example.com, except the harmless ones (see HARMLESS_EMAIL)."""
    pieces, position = [], 0
    for run in LOCAL_RUN.finditer(text):
        start, at = max(run.start(), position), run.end()  # a run may begin inside the address just masked
        if start >= at:
            continue
        domain = DOMAIN.match(text, at + 1) if text.startswith("@", at) else None
        if domain:
            address = text[start:domain.end()]
            pieces += [text[position:start], address if HARMLESS_EMAIL.search(address) else "user@example.com"]
            position = domain.end()
    return "".join(pieces) + text[position:]


WORKFLOW = re.compile(
    r"gh auth|GH_TOKEN|credential\.helper|gh account|personal account|active gh|never switch|"
    r"push only|GIT_AUTHOR|GIT_COMMITTER|never print tokens|\bgh\s+token\b|auth header|GitHub CLI authorization",
    re.I,
)
# "Near" is a window of 300 characters on the same line: with `.*` this was quadratic on a long line (15 seconds for
# 240 KB, minutes for 2 MB) and the push would look hung. An account farther than that from the trigger word is still
# masked; only the line is no longer dropped.
_NEAR = r"[^\n]{0,300}"
ACCOUNT_WORKFLOW = re.compile(
    _ACC + _NEAR + r"\b(token|credential|identity|push|commit as)\b|\b(token|credential|identity|push)\b" + _NEAR + _ACC,
    re.I,
)
# Words that a real random token rarely holds (measured on random letters and digits: about 1 token in 15,000, mostly
# "your" and "fake") and an example always does.
_TOKEN_EXAMPLE_WORDS = ("example", "your", "placeholder", "replace", "insert", "dummy", "sample", "fake", "redacted",
                        "changeme", "goeshere")


def _placeholder(token: str) -> bool:
    """A token that only shows the SHAPE of one (sk_test_xxxxxxxx, T00000000/B00000000/XXXXXXXX): the last sixteen
    letters and digits use at most three different characters, which a real token practically never does. The keys
    that the documentation of AWS and others publish say EXAMPLE in them."""
    tail = re.sub(r"[^A-Za-z0-9]", "", token)[-16:]
    return len(set(tail.lower())) <= 3 or any(word in token.lower() for word in _TOKEN_EXAMPLE_WORDS)


# A password made of one of these words is an example in a README or a test, not a secret; a real one does not read
# like a word. (Measured on random letters and digits, a real random password holds one of them by chance in about 1
# of 2,500 at 12 characters and 1 of 1,200 at 24: a deliberate cost of not refusing `postgres://svc:passw0rdExample1@db`
# in a README. The README says so.)
_EXAMPLE_WORDS = ("password", "passwd", "pass", "pwd", "secret", "changeme", "example", "sample", "demo", "test", "your",
                  "admin", "root", "guest", "user", "default", "postgres", "mysql", "mongo", "redis", "qwerty",
                  "letmein", "1234", "abcd")


def _character_class(char: str) -> str:
    return "U" if char.isupper() else "l" if char.islower() else "d" if char.isdigit() else "o"


def _not_a_real_password(match: str) -> bool:
    """True for a match of "url with a password" that is an example: an image pinned by digest (docker://...), a
    password with an example word in it, or one that reads like a word (few changes between capitals, small letters,
    digits and symbols; a random password changes class about every other character)."""
    if match.lower().startswith(("docker://", "oci://")):
        return True
    # scheme://user:password@host: the user has no colon and the password may, so the password starts at the FIRST one
    password = match.split("://", 1)[-1].rsplit("@", 1)[0].split(":", 1)[1]
    if any(word in password.lower() for word in _EXAMPLE_WORDS):
        return True
    return sum(1 for a, b in zip(password, password[1:]) if _character_class(a) != _character_class(b)) < 5


class SecretShape:
    """A secret pattern that ignores the examples of documentation. It answers like a compiled regular expression to
    the two calls the final check makes (search and findall)."""

    def __init__(self, pattern: str, flags: int = 0, example=None):
        self.regex = re.compile(pattern, flags)
        self.example = example  # an extra rule that says a match is an example, for the patterns that need one

    def findall(self, text: str) -> list:
        return [
            found.group(0)
            for found in self.regex.finditer(text)
            if not _placeholder(found.group(0)) and not (self.example and self.example(found.group(0)))
        ]

    def search(self, text: str) -> bool:
        return bool(self.findall(text))


SECRET_PATTERNS = {
    "github token": SecretShape(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "api key": SecretShape(r"\b(?:sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})"),
    "bearer / jwt": SecretShape(r"(?:Bearer\s+[A-Za-z0-9._-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.)"),
    # The header AND a body of at least 40 base64 characters within 200 characters: prose that only names the header is not a key.
    "private key block": SecretShape(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]{0,200}?[A-Za-z0-9+/=]{40,}"),
    # user:password@host in an address. The password must look like one: twelve characters or more, letters and
    # digits, no example word in it, and random-looking (see _not_a_real_password); a placeholder such as <token>,
    # ${VAR}, [password] or xxxx is not one, nor is an image pinned by digest (docker://node:20@sha256:...). So
    # `postgres:postgres@localhost` or `mypassword123` in a README is not refused. The scheme cannot start in the middle
    # of a word (that was quadratic on a long run).
    "url with a password": SecretShape(
        r"(?<![A-Za-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s:/@]+:(?![<${\[*]|x{3,})(?=[^\s@/?#]*\d)(?=[^\s@/?#]*[A-Za-z])[^\s@/?#]{12,}@(?!sha256:)",
        re.I,
        example=_not_a_real_password,
    ),
    "stripe key": SecretShape(r"\b[sr]k_(?:live|test)_(?!4eC39HqLyjWDarjtT1zdp7dc)[A-Za-z0-9]{16,}"),  # that one is Stripe's public example
    "stripe webhook secret": SecretShape(r"\bwhsec_[A-Za-z0-9]{24,}"),
    "google api key": SecretShape(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])"),  # exactly 39 characters: a long identifier is not one
    "slack webhook": SecretShape(r"hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{16,}"),
    "azure storage key": SecretShape(r"(?:Account|SharedAccess)Key=[A-Za-z0-9+/]{40,}", re.I),
    "npm token": SecretShape(r"\bnpm_[A-Za-z0-9]{30,}"),
    "sendgrid key": SecretShape(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"),  # exactly 22 and 43: a .NET namespace is not one
    "aws secret assignment": SecretShape(r"(?i)\baws_secret_access_key[\"']?\s*[:=]\s*[\"']?(?!\$)[A-Za-z0-9/+]{30,}"),
    "secret assignment": SecretShape(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*['\"]?"
        r"(?!process\.env|env\.|os\.environ|\$)[A-Za-z0-9/+_.-]{12,}"
    ),
}
FORBIDDEN = {
    "personal email": PERSONAL_EMAIL,
    "work identity": WORK_IDENTITY,
    "github account": ACCOUNT,
    "person name": NAME,
    "username": USERNAME,
    "home path": HOME_PATH,
    **SECRET_PATTERNS,
}


def scrub_text(text: str) -> tuple[str, int, bool]:
    """Drop the lines about one person's own workflow, replace the identifiers. Returns (text, dropped lines, personal)."""
    kept, dropped = [], 0
    for line in text.split("\n"):
        if WORK_IDENTITY.search(line) or WORKFLOW.search(line) or ACCOUNT_WORKFLOW.search(line):
            dropped += 1
            continue
        kept.append(line)
    out = "\n".join(kept)
    personal = bool(NAME.search(out))
    out = PERSONAL_EMAIL.sub("user@example.com", out)
    out = mask_emails(out)
    out = HOME_PATH.sub("~", out)
    out = USERNAME.sub("user", out)
    out = NAME.sub("the author", out)
    out = ACCOUNT.sub("<owner-account>", out)
    return out, dropped, personal or dropped > 0


def _relation_payload(mutation: dict) -> dict | None:
    """The decoded payload of a relation mutation (Engram stores it as a JSON string); None when it cannot be read."""
    payload = mutation.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    return payload if isinstance(payload, dict) else None


def clean_relations(mutations: list, observation_ids: set) -> tuple[list, int, int]:
    """Keep a relation only when both of its observations are in the same chunk, and blank its free-text reason
    (it can describe an observation that was left out). Other mutations pass through unchanged.
    Returns (mutations, relations dropped, reasons blanked)."""
    kept, dropped, blanked = [], 0, 0
    for mutation in mutations:
        if not (isinstance(mutation, dict) and mutation.get("entity") == "relation"):
            kept.append(mutation)
            continue
        payload = _relation_payload(mutation)
        ends = [] if payload is None else [payload.get("source_id"), payload.get("target_id")]
        if len(ends) != 2 or not all(isinstance(end, str) and end in observation_ids for end in ends):
            dropped += 1  # an end outside the chunk, or an end that cannot be read: it cannot be verified, so it goes
            continue
        if payload.get("reason"):
            payload["reason"] = ""
            if isinstance(mutation["payload"], str):
                mutation["payload"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            blanked += 1
        kept.append(mutation)
    return kept, dropped, blanked


def clean(data: dict) -> tuple[dict, dict]:
    # Engram writes null, not [], for a list with nothing in it (for example an export with no new sessions).
    for key in ("sessions", "observations", "prompts", "mutations"):
        data[key] = data.get(key) or []
    stats = {"prompts removed": len(data["prompts"]), "lines dropped": 0, "observations dropped": 0,
             "relations dropped": 0, "reasons blanked": 0}
    data["prompts"] = []
    observations = []
    for obs in data["observations"]:
        title, _, _ = scrub_text(obs.get("title") or "")
        content, dropped, personal = scrub_text(obs.get("content") or "")
        stats["lines dropped"] += dropped
        if obs.get("type") == "preference" and personal:
            stats["observations dropped"] += 1
            continue
        obs["title"], obs["content"] = title, content
        observations.append(obs)
    data["observations"] = observations
    # The relations are checked against the observations that STAY, so one that pointed to an excluded one goes too.
    kept_ids = {obs["sync_id"] for obs in observations if obs.get("sync_id")}
    data["mutations"], stats["relations dropped"], stats["reasons blanked"] = clean_relations(data["mutations"], kept_ids)
    for session in data["sessions"]:
        session["directory"], _, _ = scrub_text(session.get("directory") or "")
    return data, stats


def leftovers(text: str) -> dict[str, int]:
    return {name: len(rx.findall(text)) for name, rx in FORBIDDEN.items() if rx.search(text)}


def _strings(value):
    """Every text of a decoded JSON value: the keys and the strings, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _merge(found: dict, more: dict) -> dict:
    for name, count in more.items():
        found[name] = found.get(name, 0) + count
    return found


def where_found(data: dict, limit: int = 3) -> list:
    """Where the final check found something, without ever saying WHAT: the kind of item, its id and the field."""
    spots = []
    for kind in ("observations", "sessions", "mutations"):
        for index, item in enumerate(data.get(kind) or []):
            if not isinstance(item, dict):
                continue
            for field, value in item.items():
                found = leftovers_in(value)
                if found:
                    spots.append(f"{kind[:-1]} {item.get('id', index)}, field {field}: {', '.join(sorted(found))}")
    return spots[:limit]


def leftovers_in(value, text: str = "") -> dict[str, int]:
    """What the checks find in a decoded JSON value, text by text, and in `text` (the JSON as it is written). In JSON
    text a line break inside a string is the two characters backslash and n, so a name split by a line break would
    not be seen there; in the decoded string it is a line break, and the name is found."""
    found = {}
    for item in _strings(value):
        _merge(found, leftovers(item))
    for name, count in (leftovers(text) if text else {}).items():
        found.setdefault(name, count)
    return found


MANIFEST_FIELDS = {"id", "created_by", "created_at", "sessions", "memories", "prompts"}


def manifest_leftovers(manifest: dict) -> dict[str, int]:
    """The checks on the manifest, without its own vocabulary: the names of its fields and the placeholder `owner`
    that the blanking writes are not personal data, even when somebody's username happens to be `owner` or `version`
    (otherwise the cleaning could never finish)."""
    texts = []
    for key, value in manifest.items():
        if key != "chunks":
            texts += [key] if key != "version" else []
            texts += list(_strings(value))
    for entry in manifest.get("chunks", []):
        for key, value in entry.items():
            if key not in MANIFEST_FIELDS:
                texts.append(key)
            if value != "owner":
                texts += list(_strings(value))
    found = {}
    for text in texts:
        _merge(found, leftovers(text))
    return found


def unpublished_chunk_ids(repo: Path) -> set[str] | None:
    """Ids of the chunk files that git does not have committed yet (what an export on THIS machine just made).
    None when `repo` is not a git repository, which means: treat every chunk as new."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all", "--", ".engram/chunks"],
            capture_output=True,
            encoding="utf-8",  # strict on purpose: a path that cannot be read must stop the cleaning, never be skipped
        )
    except OSError:  # no git on this machine
        return None
    if done.returncode != 0:
        return None
    ids = set()
    for line in done.stdout.splitlines():
        name = line[3:].strip().strip('"').split("/")[-1]
        if name.endswith(".jsonl.gz"):
            ids.add(name[: -len(".jsonl.gz")])
    return ids


TEMP_SUFFIX = ".sanitize-tmp"


def write_atomic(path: Path, data: bytes) -> None:
    """Write `data` so that `path` is either the old file or the complete new one, never a half-written file."""
    temp = path.with_name(path.name + TEMP_SUFFIX)
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _clean_repository(repo: Path, only_new: bool = False) -> int:
    """Clean the chunks. With only_new, the chunks that are already committed (published by this or another machine)
    are left exactly as they are: this machine's identifiers are not theirs, and cleaning them would rewrite them.

    Phase 1 decides everything in memory (a STOP there writes and moves nothing); phase 2 writes the new chunks,
    replaces the manifest atomically and only then moves the previous files away."""
    engram = repo / ".engram"
    chunks_dir = engram / "chunks"
    manifest_path = engram / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    original_manifest = json.loads(json.dumps(manifest))
    unpublished = unpublished_chunk_ids(repo)
    new_only = unpublished if only_new else None

    # ---- phase 1: read, clean and verify every chunk; nothing is written or moved here ----
    kept = []
    notes = []  # what to print once the work is really done, in the order of the manifest
    new_files = {}  # new chunk id -> compressed bytes
    old_paths = []  # the previous files of the chunks that were rewritten or dropped
    for entry in manifest["chunks"]:
        old_id = entry["id"]
        if new_only is not None and old_id not in new_only:
            notes.append(f"chunk {old_id}: already published, left as it is")
            kept.append(entry)
            continue
        path = chunks_dir / f"{old_id}.jsonl.gz"
        plain = gzip.decompress(path.read_bytes()).decode("utf-8-sig")
        data, stats = clean(json.loads(plain))
        cleaned = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        left = leftovers_in(data, cleaned)
        if left:
            print(f"STOP: chunk {old_id} still has personal data after cleaning, nothing was written: {left}")
            for spot in where_found(data) or ["no single value holds it; it shows only in the text of the chunk as a whole (for example in the name of a field)"]:
                print(f"  where: {spot}")
            return 1
        if not (data["sessions"] or data["observations"] or data["mutations"]):
            # An export that only carried prompts is empty once they are gone: sharing it would be noise.
            old_paths.append(path)
            notes.append(f"chunk {old_id}: nothing left to share after cleaning, dropped (kept only in {LOCAL_ONLY})")
            continue
        if cleaned == plain:
            notes.append(f"chunk {old_id}: already clean")
        else:
            new_id = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
            new_files[new_id] = gzip.compress(cleaned.encode("utf-8"), 9, mtime=0)
            old_paths.append(path)
            entry["id"], entry["prompts"], entry["created_by"] = new_id, 0, "owner"
            entry["memories"] = len(data["observations"])
            notes.append(f"chunk {old_id} -> {new_id}: {stats}")
            notes.append(f"  previous file kept only in {LOCAL_ONLY}")
        kept.append(entry)
        notes.append(
            f"  contents: {len(data['sessions'])} sessions, {len(data['observations'])} observations, "
            f"{len(data['prompts'])} prompts, {len(data['mutations'])} relations"
        )
    manifest["chunks"] = kept
    # The manifest is checked with the same patterns as the chunks, on its decoded strings (JSON text would escape
    # an accented letter or a line break and hide the name). `created_by` is the only free text of an entry: it is
    # blanked in the entries that have something (not in the others: they describe other people's published
    # chunks), and if anything is still found afterwards nothing is written.
    if manifest_leftovers(manifest):
        for entry in manifest["chunks"]:
            if manifest_leftovers({"chunks": [entry]}):
                entry["created_by"] = "owner"
        left = manifest_leftovers(manifest)
        if left:
            print(f"STOP: the manifest still has personal data after cleaning, nothing was written: {left}")
            return 1
    changed = manifest != original_manifest

    # ---- phase 2: new files first, then the manifest in one atomic step, the previous files last ----
    referenced = {entry["id"] for entry in manifest["chunks"]}
    try:
        LOCAL_ONLY.mkdir(parents=True, exist_ok=True)
        for stale in [*engram.glob(f"*{TEMP_SUFFIX}"), *chunks_dir.glob(f"*{TEMP_SUFFIX}")]:
            stale.unlink()  # left by a run that was killed half way
        for new_id, blob in new_files.items():
            write_atomic(chunks_dir / f"{new_id}.jsonl.gz", blob)
        if changed:
            write_atomic(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
        # Also a file that a previous, interrupted run left behind: it is not in the manifest and git does not have
        # it, so it must not stay in the repository (it would be committed with the rest). This needs git's answer
        # about what is committed: inside a git repository without it, nothing is swept (a tracked chunk of someone
        # else would be moved and its deletion staged). Outside a git repository every such file is swept.
        to_move = [path for path in old_paths if path.name[: -len(".jsonl.gz")] not in referenced]
        sweep = unpublished is not None or not (repo / ".git").exists()
        if sweep:
            for path in sorted(chunks_dir.glob("*.jsonl.gz")):
                chunk_id = path.name[: -len(".jsonl.gz")]
                if chunk_id not in referenced and (unpublished is None or chunk_id in unpublished) and path not in to_move:
                    to_move.append(path)
        for path in to_move:
            if path.exists():
                shutil.move(str(path), str(LOCAL_ONLY / path.name))
    except OSError as exc:
        print(f"STOP: could not finish writing ({exc}). The manifest only lists files that exist and no data was "
              "lost: run the cleaning again to finish.")
        return 1
    for note in notes:
        print(note)
    if not sweep:
        print("sweep of unreferenced chunk files skipped: git could not report the status, so none was moved")
    print("verification: no personal data, no secrets, no prompts left in the chunk files or the manifest")
    return 0


def main(repo: Path, only_new: bool = False) -> int:
    """Clean the chunks of `repo` (see `_clean_repository`). The folding cache holds copies of the texts it saw before
    they were cleaned, so it is emptied when the run ends, whatever the outcome."""
    try:
        return _clean_repository(repo, only_new)
    finally:
        _fold.cache_clear()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--only-new"]
    if len(args) != 1 or len(sys.argv) - len(args) > 2:
        sys.exit(__doc__)
    sys.exit(main(Path(args[0]).resolve(), only_new="--only-new" in sys.argv[1:]))
