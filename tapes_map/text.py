"""Cleaning for prompts and titles: strips what harnesses wrap around the words a person typed."""

import re

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
# A paired tag and everything inside it, across lines. Applied until nothing matches, so nested blocks go too.
_TAG_BLOCK = re.compile(r"<([A-Za-z][\w:.-]*)(?:\s[^<>]*)?>.*?</\1\s*>", re.DOTALL)
_LONE_TAG = re.compile(r"</?[A-Za-z][\w:.-]*(?:\s[^<>]*)?/?>")
# [T3 Code: ...], [Codex: ...]: a short bracketed name and a colon at the start of a bracket.
_PREAMBLE = re.compile(r"\[[A-Za-z0-9][\w .-]{0,40}:[^\]]*\]")
# Session titles split from a multi-part prompt end in "(part 2/4)".
_PART = re.compile(r"\(part \d+/\d+\)", re.IGNORECASE)
_URL = re.compile(r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)\S+", re.IGNORECASE)
_UUID = re.compile(r"\b[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\b", re.IGNORECASE)
# Hex ids such as commit SHAs: seven or more hex digits with at least one digit, so words like "defaced" stay.
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.IGNORECASE)
# Anything with a slash between path characters (src/a.py, /tmp/x, ~/.config/y, @plans/z), then bare file names.
_PATH = re.compile(r"(?<![\w/])[~@.]*[\w.@+-]*/[\w.@+~/-]*")
_FILE = re.compile(
    r"\b[\w.-]+\.(?:md|mdx|py|ts|tsx|js|jsx|mjs|cjs|json|jsonl|toml|ya?ml|go|rs|sql|sh|txt|png|jpe?g|gif|svg|css|html|lock|log|env|tf)\b",
    re.IGNORECASE,
)
_SPACE = re.compile(r"\s+")


def clean(text: str | None) -> str:
    """The text without harness markup, links, paths, ids and code, on one line."""
    if not text:
        return ""
    text = _FENCE.sub(" ", text)
    while True:
        stripped = _TAG_BLOCK.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    for pattern in (_LONE_TAG, _PREAMBLE, _PART, _URL, _UUID, _PATH, _FILE, _HEX):
        text = pattern.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def truncate(text: str, limit: int) -> str:
    """At most `limit` characters, ending in an ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"

