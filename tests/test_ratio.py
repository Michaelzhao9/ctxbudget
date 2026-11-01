"""The declared window, the declared trigger, and the measurement that outvotes them.

``--postcompact`` divides the per-turn cost by a window the client *left behind*
(a measured number). ``--ratio`` answers a different question: what does the
*declared* budget mean once the per-turn cost is in it, and does the declaration
survive the receipts that were actually written.

Every test here owns one rule:

1. a ratio prints both of its numbers, and the share never rounds to ``0.00%``;
2. a declared window smaller than what the receipts measured is a contradiction
   (``window_below_measured``), not a small ratio;
3. a declared trigger is compared to the measured median ``preTokens`` and is
   called out when it diverges beyond the tolerance;
4. nothing measured means nothing declared-against-measured: the verdict is
   ``no_measurement`` and no divergence is invented.
"""

from ctxbudget.ratio import ratio_payload, render_ratio_markdown


def test_ratio_prints_both_numbers_and_the_turns_it_buys():
    payload = ratio_payload(per_turn=614, window=200000)

    assert payload["ratios"]["per_turn_over_window"] == 614 / 200000
    assert payload["ratios"]["turns_to_window"] == 325  # floor(200000 / 614)
    assert payload["verdict"] == "no_measurement"
    assert payload["declared"]["window_source"] == "flag"

    text = render_ratio_markdown(payload)
    assert "614 / 200,000" in text
    assert "0.31%" in text
    assert "325 turns" in text


def test_an_empty_project_has_no_turn_count_instead_of_dividing_by_zero():
    payload = ratio_payload(per_turn=0, window=200000, trigger=0.8)

    assert payload["ratios"]["per_turn_over_window"] == 0
    assert payload["ratios"]["turns_to_window"] is None
    assert payload["ratios"]["turns_to_threshold"] is None
    text = render_ratio_markdown(payload)
    assert "n/a turns" in text


def test_a_window_smaller_than_the_measured_pre_compaction_is_a_contradiction():
    measurement = {"median_pre": 967232, "boundaries": 68, "root": "~/projects"}

    payload = ratio_payload(
        per_turn=614, window=200000, trigger=0.8, measurement=measurement
    )

    assert payload["verdict"] == "window_below_measured"
    text = render_ratio_markdown(payload)
    assert "967,232" in text and "200,000" in text
    assert "window_below_measured" in text
    # the declared threshold is not compared to a measurement the window cannot hold
    assert payload["divergence"] is None


def test_a_declared_trigger_that_the_receipts_confirm_says_so():
    measurement = {"median_pre": 160000, "boundaries": 12, "root": "~/projects"}

    payload = ratio_payload(
        per_turn=2000, window=200000, trigger=0.8, measurement=measurement
    )

    assert payload["verdict"] == "threshold_confirmed"
    assert payload["divergence"]["declared_tokens"] == 160000
    assert abs(payload["divergence"]["relative"]) < 0.10


def test_a_declared_trigger_off_by_more_than_tolerance_diverges():
    measurement = {"median_pre": 27418, "boundaries": 68, "root": "~/projects"}

    payload = ratio_payload(
        per_turn=614, window=200000, trigger=0.8, measurement=measurement
    )

    assert payload["verdict"] == "threshold_diverges"
    assert payload["divergence"]["relative"] > 0.10
    text = render_ratio_markdown(payload)
    assert "27418" not in text  # thousands separators, always
    assert "27,418" in text
    assert "+483" in text or "+484" in text


def test_without_a_trigger_the_measurement_is_printed_and_no_divergence_claimed():
    measurement = {"median_pre": 27418, "boundaries": 68, "root": "~/projects"}

    payload = ratio_payload(per_turn=614, window=200000, measurement=measurement)

    assert payload["verdict"] == "threshold_absent"
    assert payload["divergence"] is None
    assert payload["measured"]["median_pre"] == 27418


def test_a_share_too_small_to_round_never_prints_as_zero():
    payload = ratio_payload(per_turn=1, window=1000000)

    text = render_ratio_markdown(payload)
    assert "0.00%" not in text
    assert "<0.01%" in text


def test_the_trigger_sentence_reads_without_a_vowel_clash():
    text = render_ratio_markdown(ratio_payload(per_turn=614, window=200000, trigger=0.8))

    assert "a trigger at 80.0% of the window is 160,000 tokens" in text
    assert "a 80.0% trigger" not in text
