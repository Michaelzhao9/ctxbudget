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

from ctxbudget.ratio import (
    measurement_from_compactions,
    ratio_payload,
    render_ratio_markdown,
)


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


def _mixed_measurement():
    return {
        "median_pre": 600_000,
        "boundaries": 4,
        "root": "~/projects",
        "by_trigger": {
            "auto": {
                "boundaries": 2,
                "median_pre": 910_000,
                "min_pre": 900_000,
                "max_pre": 920_000,
            },
            "manual": {
                "boundaries": 1,
                "median_pre": 200_000,
                "min_pre": 200_000,
                "max_pre": 200_000,
            },
            "unrecorded": {
                "boundaries": 1,
                "median_pre": 300_000,
                "min_pre": 300_000,
                "max_pre": 300_000,
            },
        },
    }


def test_the_declared_trigger_is_compared_with_auto_not_with_the_mixture():
    payload = ratio_payload(
        per_turn=614, window=1_000_000, trigger=0.9, measurement=_mixed_measurement()
    )

    assert payload["measured"]["trigger"] == "auto"
    assert payload["measured"]["median_pre"] == 910_000
    assert payload["measured"]["group_boundaries"] == 2
    assert payload["divergence"]["measured_tokens"] == 910_000
    assert payload["verdict"] == "threshold_confirmed"  # 900,000 vs 910,000 = -1.1%

    text = render_ratio_markdown(payload)
    assert "auto" in text and "2 of 4" in text
    assert "200,000" in text  # the manual group is printed, not hidden


def test_two_trigger_values_and_no_auto_refuse_the_comparison():
    measurement = {
        "median_pre": 500_000,
        "boundaries": 3,
        "root": "~/projects",
        "by_trigger": {
            "manual": {
                "boundaries": 1,
                "median_pre": 600_000,
                "min_pre": 600_000,
                "max_pre": 600_000,
            },
            "unknown": {
                "boundaries": 2,
                "median_pre": 450_000,
                "min_pre": 400_000,
                "max_pre": 500_000,
            },
        },
    }
    payload = ratio_payload(
        per_turn=614, window=1_000_000, trigger=0.9, measurement=measurement
    )

    assert payload["verdict"] == "mixed_triggers"
    assert payload["divergence"] is None
    assert any("mix" in note for note in payload["notes"])
    text = render_ratio_markdown(payload)
    assert "manual" in text and "unknown" in text


def test_a_single_group_is_used_even_when_it_is_not_auto():
    measurement = {
        "median_pre": 700_000,
        "boundaries": 2,
        "root": "~/projects",
        "by_trigger": {
            "manual": {
                "boundaries": 2,
                "median_pre": 700_000,
                "min_pre": 600_000,
                "max_pre": 800_000,
            }
        },
    }
    payload = ratio_payload(
        per_turn=614, window=1_000_000, trigger=0.9, measurement=measurement
    )

    assert payload["measured"]["trigger"] == "manual"
    assert payload["divergence"]["measured_tokens"] == 700_000
    assert payload["verdict"] == "threshold_diverges"  # 900,000 vs 700,000 = +28.6%
    assert "manual" in render_ratio_markdown(payload)


def test_measurement_carries_the_trigger_groups_out_of_the_receipts():
    measurement = measurement_from_compactions(
        {
            "window": {
                "median_pre": 600_000,
                "by_trigger": {
                    "auto": {
                        "boundaries": 2,
                        "median_pre": 910_000,
                        "min_pre": 900_000,
                        "max_pre": 920_000,
                    }
                },
            },
            "totals": {"with_pre_and_post": 2},
            "root": "~/projects",
        }
    )

    assert measurement["by_trigger"]["auto"]["median_pre"] == 910_000
    assert measurement["boundaries"] == 2


def test_a_single_trigger_value_says_the_grouping_cannot_move_the_median():
    measurement = {
        "median_pre": 967_169,
        "boundaries": 85,
        "root": "~/projects",
        "by_trigger": {
            "auto": {
                "boundaries": 85,
                "median_pre": 967_169,
                "min_pre": 207_680,
                "max_pre": 997_904,
            }
        },
    }
    payload = ratio_payload(
        per_turn=189, window=1_000_000, trigger=0.9, measurement=measurement
    )

    text = render_ratio_markdown(payload)
    assert "only one trigger value" in text
    assert "85 of 85" in text


def _tolerance_measurement():
    """A group whose median sits inside the band while a quarter of it does not."""
    return {
        "median_pre": 910_000,
        "boundaries": 4,
        "root": "~/projects",
        "by_trigger": {
            "auto": {
                "boundaries": 4,
                "median_pre": 910_000,
                "min_pre": 850_000,
                "max_pre": 1_300_000,
            }
        },
        "pre_by_trigger": {"auto": [850_000, 870_000, 950_000, 1_300_000]},
    }


def test_measurement_keeps_per_boundary_pre_values_grouped_by_trigger():
    measurement = measurement_from_compactions(
        {
            "window": {"median_pre": 600_000, "by_trigger": {}},
            "totals": {"with_pre_and_post": 3},
            "root": "~/projects",
            "boundaries": [
                {"pre": 910_000, "post": 100_000, "trigger": "auto"},
                {"pre": 930_000, "post": 90_000, "trigger": "auto"},
                {"pre": 200_000, "post": 20_000, "trigger": "manual"},
                {"pre": 300_000, "post": 30_000, "trigger": None},
            ],
        }
    )

    assert measurement["pre_by_trigger"]["auto"] == [910_000, 930_000]
    assert measurement["pre_by_trigger"]["manual"] == [200_000]
    assert measurement["pre_by_trigger"]["unrecorded"] == [300_000]


def test_a_confirmed_median_reports_how_many_boundaries_agree():
    payload = ratio_payload(
        per_turn=189,
        window=1_000_000,
        trigger=0.9,
        measurement=_tolerance_measurement(),
    )

    assert payload["verdict"] == "threshold_confirmed"
    divergence = payload["divergence"]
    assert divergence["within_tolerance"] == 3
    assert divergence["boundaries_counted"] == 4
    assert any("3 of 4" in note for note in payload["notes"])

    text = render_ratio_markdown(payload)
    assert "3 of 4" in text
    assert "1,300,000" not in text  # the outlier is counted, not enumerated


def test_a_count_is_absent_rather_than_invented_when_the_values_are_missing():
    measurement = {"median_pre": 910_000, "boundaries": 4, "root": "~/projects"}
    payload = ratio_payload(
        per_turn=189, window=1_000_000, trigger=0.9, measurement=measurement
    )

    assert payload["divergence"]["within_tolerance"] is None
    assert payload["divergence"]["boundaries_counted"] is None
    assert not any("of 4" in note for note in payload["notes"])
    text = render_ratio_markdown(payload)
    assert "within" not in text


def test_a_diverging_median_still_reports_the_count_it_is_based_on():
    payload = ratio_payload(
        per_turn=189, window=1_000_000, trigger=0.7, measurement=_tolerance_measurement()
    )

    assert payload["verdict"] == "threshold_diverges"  # 700,000 vs 910,000
    assert payload["divergence"]["within_tolerance"] == 0
    assert payload["divergence"]["boundaries_counted"] == 4
    assert not any("median lands inside" in note for note in payload["notes"])


def _uneven_population():
    """A trigger group whose median population and pair population differ.

    Five boundaries exist: four carry ``auto`` and one carries no trigger; three
    of the four ``auto`` rows also carry ``postTokens``. Any sentence that puts
    one of those counts next to a median taken over another is a false statement.
    """
    return {
        "median_pre": 925_000,
        "boundaries": 4,
        "boundaries_total": 5,
        "boundaries_with_post": 3,
        "root": "~/projects",
        "by_trigger": {
            "auto": {
                "boundaries": 4,
                "median_pre": 925_000,
                "min_pre": 200_000,
                "max_pre": 1_000_000,
            },
            "unrecorded": {
                "boundaries": 1,
                "median_pre": 250_000,
                "min_pre": 250_000,
                "max_pre": 250_000,
            },
        },
        "pre_by_trigger": {"auto": [900_000, 950_000, 1_000_000, 200_000]},
        "pairs_by_trigger": {"auto": 3},
    }


def test_the_group_median_names_the_population_it_was_taken_over():
    payload = ratio_payload(
        per_turn=189, window=1_000_000, trigger=0.9, measurement=_uneven_population()
    )

    measured = payload["measured"]
    assert measured["boundaries"] == 4  # median over every auto row that has pre
    assert measured["group_boundaries"] == 4
    assert measured["boundaries_total"] == 5

    text = render_ratio_markdown(payload)
    assert "over 4 boundaries" in text
    assert "4 of 3" not in text


def _uneven_receipts():
    """The same population, extracted from receipts rather than hand-built."""
    return measurement_from_compactions(
        {
            "root": "~/projects",
            "window": {
                "median_pre": 950_000,
                "by_trigger": {
                    "auto": {"boundaries": 4, "median_pre": 925_000},
                    "unrecorded": {"boundaries": 1, "median_pre": 250_000},
                },
            },
            "totals": {"with_pre_and_post": 3},
            "boundaries": [
                {"pre": 900_000, "post": 90_000, "trigger": "auto"},
                {"pre": 950_000, "post": 95_000, "trigger": "auto"},
                {"pre": 1_000_000, "post": 100_000, "trigger": "auto"},
                {"pre": 200_000, "post": None, "trigger": "auto"},
                {"pre": 250_000, "post": None, "trigger": None},
            ],
        }
    )


def test_the_group_share_never_claims_more_boundaries_than_exist():
    text = render_ratio_markdown(
        ratio_payload(
            per_turn=189,
            window=1_000_000,
            trigger=0.9,
            measurement=_uneven_receipts(),
        )
    )
    line = next(line for line in text.splitlines() if "trigger group:" in line)
    share = line.split("—")[1].split("boundar")[0].strip()
    counted, total = (int(part) for part in share.split(" of "))
    assert counted <= total


def test_rows_without_a_post_count_say_so_when_the_count_uses_them():
    text = render_ratio_markdown(
        ratio_payload(
            per_turn=189,
            window=1_000_000,
            trigger=0.9,
            measurement=_uneven_population(),
        )
    )

    assert "3 of those 4" in text  # pairs among the counted population
    even = render_ratio_markdown(
        ratio_payload(
            per_turn=189, window=1_000_000, trigger=0.9, measurement=_tolerance_measurement()
        )
    )
    assert "carry postTokens" not in even  # equal populations need no caveat


def test_a_hand_built_measurement_does_not_invent_a_pair_count():
    payload = ratio_payload(
        per_turn=189,
        window=1_000_000,
        trigger=0.9,
        measurement={"median_pre": 910_000, "boundaries": 4, "root": "~/projects"},
    )

    assert payload["measured"]["boundaries_with_post"] is None
    assert payload["measured"]["boundaries_total"] is None
    text = render_ratio_markdown(payload)
    assert "carry postTokens" not in text
    assert "over 4 boundaries" in text  # the given population is the only one known


def test_the_report_names_the_fraction_of_the_window_the_median_implies():
    """The declared fraction is not the only fraction in the room.

    A reader who is told the median lands at 96.7% of their window has to do the
    division themselves to learn which ``--trigger`` would have described it. The
    number is one division over two numbers the report already prints.
    """
    payload = ratio_payload(
        per_turn=614,
        window=1_000_000,
        trigger=0.95,
        measurement={"median_pre": 967_169, "boundaries": 85, "root": "~/projects"},
    )

    implied = payload["measured"]["implied_fraction"]
    assert abs(implied - 0.967169) < 1e-9
    text = render_ratio_markdown(payload)
    assert "implied trigger: 96.7%" in text
    assert "you declared 95.0%" in text


def test_no_implied_fraction_is_invented_without_a_measurement():
    payload = ratio_payload(per_turn=614, window=1_000_000, trigger=0.95)

    assert payload["measured"] is None
    assert "implied" not in render_ratio_markdown(payload)


def test_a_median_above_the_window_implies_more_than_one_window():
    """The implied fraction must not be clamped at 100%: it is a ratio, not a share."""
    payload = ratio_payload(
        per_turn=614,
        window=200_000,
        trigger=0.8,
        measurement={"median_pre": 967_169, "boundaries": 85, "root": "~/projects"},
    )

    assert payload["verdict"] == "window_below_measured"
    assert payload["measured"]["implied_fraction"] > 1.0
    assert "implied trigger: 483.6%" in render_ratio_markdown(payload)


def test_the_implied_fraction_does_not_claim_a_declared_trigger_that_was_absent():
    payload = ratio_payload(
        per_turn=614,
        window=1_000_000,
        measurement={"median_pre": 500_000, "boundaries": 10, "root": "~/projects"},
    )

    text = render_ratio_markdown(payload)
    assert "implied trigger: 50.0%" in text
    assert "you declared" not in text
