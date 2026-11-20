import pytest

from tapes_map.text import clean, tokens, truncate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<system-reminder>\nSecret harness text\n</system-reminder>\nRename the button", "Rename the button"),
        ('<teammate-message teammate_id="lead" color="blue">\nDo the other thing\n</teammate-message> then ship', "then ship"),
        ("<codex_delegation>\n<inner>x</inner>\nmore\n</codex_delegation>Real ask", "Real ask"),
        ("before <br/> after </div>", "before after"),
        ("[T3 Code: thread abc, worktree /x] Make the map faster", "Make the map faster"),
        ("Read https://github.com/a/b/pull/12 and www.example.com/x now", "Read and now"),
        ("Look at src/tapes_map/store.py and ~/.config/x and @plans/a/ and README.md", "Look at and and and"),
        ("Commit 6d9bce8a and session 01a12823-c4a6-798b-92b4-4f87e0248829 defaced", "Commit and session defaced"),
        ("Run this:\n```bash\nrm -rf /\n```\nthen stop", "Run this: then stop"),
        ("unterminated ```python\nprint(1)", "unterminated"),
        ("a <b> c", "a c"),
        ("Review issue 2659 (part 4/4)", "Review issue 2659"),
        ("x < y and y > z", "x < y and y > z"),
        (None, ""),
    ],
)
def test_clean_drops_harness_markup(raw, expected):
    assert clean(raw) == expected


def test_tokens_are_lowercase_three_letter_words_without_stopwords():
    text = "Please fix the Kubernetes CI in <tag>Hidden</tag> https://x.dev/ok and add Grafana dashboards, OK? id 1234567f"
    assert tokens(text) == ["kubernetes", "grafana", "dashboards"]


def test_truncate_marks_the_cut():
    assert truncate("short", 10) == "short"
    assert truncate("a" * 12, 10) == "a" * 9 + "…"
    assert len(truncate("word " * 50, 160)) <= 160
