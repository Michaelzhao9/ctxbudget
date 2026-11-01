from ctxbudget.dupes import find_duplicates
from ctxbudget.discover import Source


def make(label, text, tokens=None):
    return Source(
        kind="instruction",
        label=label,
        path="/tmp/" + label,
        scope="project",
        text=text,
        tokens=tokens if tokens is not None else len(text),
        per_turn=True,
    )


LONG_SHARED = (
    "Always run the full test suite before committing and never skip the linter, "
    "and always report the exact command output in the pull request body."
)


def test_identical_files_are_flagged_as_fully_redundant():
    a = make("a.md", LONG_SHARED)
    b = make("b.md", LONG_SHARED)
    dupes = find_duplicates([a, b])
    assert len(dupes) == 1
    assert dupes[0]["similarity"] == 1.0
    assert dupes[0]["redundant_tokens"] == b.tokens
    assert {dupes[0]["a"], dupes[0]["b"]} == {"a.md", "b.md"}


def test_unrelated_files_are_not_flagged():
    a = make("a.md", LONG_SHARED)
    b = make("b.md", "Use tabs for indentation in the legacy Fortran module only.")
    assert find_duplicates([a, b]) == []


def test_short_sources_are_ignored_to_avoid_noise():
    a = make("a.md", "Be careful.")
    b = make("b.md", "Be careful.")
    assert find_duplicates([a, b]) == []


def test_chinese_text_overlap_is_detected():
    text = "提交之前必须运行完整测试并贴出真实输出，禁止只写看起来合理的结论。"
    a = make("zh-a.md", text)
    b = make("zh-b.md", text)
    dupes = find_duplicates([a, b])
    assert len(dupes) == 1
    assert dupes[0]["similarity"] == 1.0


def test_on_demand_sources_are_skipped():
    a = make("a.md", LONG_SHARED)
    b = make("b.md", LONG_SHARED)
    b.per_turn = False
    assert find_duplicates([a, b]) == []


def test_results_are_sorted_by_wasted_tokens():
    a = make("a.md", LONG_SHARED, tokens=100)
    b = make("b.md", LONG_SHARED, tokens=900)
    c = make("c.md", LONG_SHARED, tokens=500)
    dupes = find_duplicates([a, b, c])
    # each pair reports the smaller side (the most that removal of one can save)
    assert dupes[0]["redundant_tokens"] == 500
    assert dupes[0]["similarity"] == 1.0
