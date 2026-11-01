import math

import pytest

from ctxbudget.tokens import (
    estimate_tokens,
    tokenizer_info,
)


def test_empty_text_costs_nothing():
    assert estimate_tokens("") == 0


def test_ascii_prose_uses_the_greater_of_the_two_heuristics():
    # 83 chars -> 20.75 by chars/4; 16 words -> 21.33 by words/0.75, so 21 wins
    text = "The quick brown fox jumps over the lazy dog while the agent reads many skill files."
    assert estimate_tokens(text) == 21


def test_more_text_never_costs_less():
    short = "one two three four five"
    long = short + " six seven eight nine ten eleven twelve thirteen"
    assert estimate_tokens(long) > estimate_tokens(short)


def test_cjk_text_is_not_priced_as_ascii():
    text = "上下文预算审计工具会对每一轮的上下文成本逐项计价。"  # 26 CJK chars
    chars_over_four = len(text) / 4
    assert estimate_tokens(text) > chars_over_four * 2


def test_tokenizer_info_is_labelled_as_an_estimate_when_tiktoken_is_absent():
    info = tokenizer_info()
    assert set(info) == {"name", "estimated", "note"}
    assert isinstance(info["estimated"], bool)
    if info["estimated"]:
        assert "heuristic" in info["name"]
        assert info["note"]


def test_estimate_is_always_an_int():
    assert isinstance(estimate_tokens("abc def"), int)
    assert not isinstance(estimate_tokens("abc def"), bool)
