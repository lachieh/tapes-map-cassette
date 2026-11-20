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
_WORD = re.compile(r"[a-z]{3,}")

STOPWORDS = frozenset(
    """
    about above after again against all also although among and another any anyone anything are aren around
    because been before being below between both but can cannot could couldn did didn does doesn doing don done
    down during each either else enough etc even ever every few for from further had hadn has hasn have haven
    having her here hers herself him himself his how however into isn its itself just least less let like
    made many may might mine more most much must mustn myself need needn neither never nor not now off once
    one only onto other others our ours ourselves out over own per quite rather really same shall shan she
    should shouldn since some something still such than that the their theirs them themselves then there
    these they this those though through thus too toward towards under until upon very via was wasn way
    were weren what whatever when where whether which while who whom whose why will with within without won
    would wouldn yes yet you your yours yourself yourselves
    """.split()
    # Agent boilerplate: words nearly every prompt to a coding agent uses.
    + """
    please use using used make makes making file files code run running runs add adding added update updating
    updated fix fixing fixed work working works task tasks agent agents subagent thread threads user users
    message messages sent send can will need needs want wants also like just now new let get gets getting
    see look looking sure okay thanks thank help try trying going think know thing things something
    act sub instructions instruction follow following read reading return result results report done
    session sessions context prompt prompts reply respond response turn turns current previous
    start first second complete
    """.split()
)


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


def tokens(text: str | None) -> list[str]:
    """Lowercase words of three or more letters from cleaned text, without stopwords."""
    return [word for word in _WORD.findall(clean(text).lower()) if word not in STOPWORDS]
