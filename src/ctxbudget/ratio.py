"""Does the budget you declared survive the window the harness actually used?

``ctxbudget`` prices what the stack injects per turn. ``--postcompact`` states
that cost as a share of the window a compaction *left behind* — a measured
number. Neither answers the question people actually configure against: what
does *my declared* window and *my declared* compaction trigger mean once the
per-turn cost sits inside them, and does the client's own receipt agree with
what I declared.

So this mode takes two numbers from the user (a window and optionally a trigger
as a fraction of it) and one optional measurement (compaction receipts), and it
refuses to fill in any of the three:

1. **The window is declared, never inferred.** A model name does not carry a
   denominator, and the receipt field that claims to is not owned by this tool.
2. **A declared window smaller than what the receipts measured is a
   contradiction, not a small ratio.** Verdict ``window_below_measured``; no
   trigger is compared against a measurement the declaration cannot hold.
3. **A ratio prints both of its numbers**, and a share that rounds below one
   hundredth of a percent prints ``<0.01%`` rather than ``0.00%``.

Read-only: measurement reads the same compaction receipts as ``--postcompact``
and keeps no message text.
"""

from typing import Optional

DEFAULT_TOLERANCE = 0.10


def _whole(value):
    """A token count is a whole number of tokens."""
    return int(round(value))


def share_text(value):
    """Percent with enough digits to see a small ratio, and no fake zero."""
    if value is None:
        return "n/a"
    pct = 100.0 * value
    if pct < 0.005:
        return "<0.01%"
    return "%.2f%%" % pct


def measurement_from_compactions(payload):
    """Extract the effective trigger from a ``scan_compactions`` payload.

    Returns ``None`` when no boundary carries both counts: an effective trigger
    that was never written is unknown, and unknown is not zero.
    """
    window = (payload or {}).get("window") or {}
    median_pre = window.get("median_pre")
    if not median_pre:
        return None
    return {
        "median_pre": median_pre,
        "boundaries": ((payload.get("totals") or {}).get("with_pre_and_post") or 0),
        "root": payload.get("root"),
        "by_trigger": window.get("by_trigger") or {},
    }


AUTO_TRIGGER = "auto"


def choose_trigger_group(by_trigger):
    """Return ``(label, group)`` for the population a declared trigger configures.

    ``auto`` is the only trigger a declared fraction can describe: a manual
    ``/compact`` is a user decision, not a threshold firing. When no ``auto``
    group exists and every boundary shares one trigger, that single group is
    used and its label is printed. Two or more groups with no ``auto`` are a
    mixture, and a declared trigger is not compared against a mixture.
    """
    if not by_trigger:
        return None, None
    if AUTO_TRIGGER in by_trigger:
        return AUTO_TRIGGER, by_trigger[AUTO_TRIGGER]
    if len(by_trigger) == 1:
        label = next(iter(by_trigger))
        return label, by_trigger[label]
    return None, None


def ratio_payload(
    per_turn,
    window,
    trigger=None,
    measurement=None,
    tolerance=DEFAULT_TOLERANCE,
    per_turn_source="flag",
):
    """Compare a declared budget with a measured compaction boundary."""
    notes = []
    per_turn = int(per_turn)
    window = int(window)

    threshold_tokens: Optional[int] = None
    if trigger is not None:
        threshold_tokens = _whole(trigger * window)

    ratios = {
        "per_turn_over_window": per_turn / window,
        "turns_to_window": window // per_turn if per_turn else None,
        "threshold_fraction": trigger,
        "threshold_tokens": threshold_tokens,
        "turns_to_threshold": (
            threshold_tokens // per_turn if threshold_tokens and per_turn else None
        ),
    }

    measured = None
    mixture = None
    if measurement and measurement.get("median_pre"):
        by_trigger = measurement.get("by_trigger") or {}
        label, group = choose_trigger_group(by_trigger)
        if by_trigger and label is None:
            mixture = by_trigger
        measured = {
            "median_pre": int(group["median_pre"]) if group else int(measurement["median_pre"]),
            "boundaries": int(measurement.get("boundaries") or 0),
            "root": measurement.get("root"),
            "basis": "median preTokens across the compaction receipts that carry it",
            "trigger": label,
            "group_boundaries": int(group["boundaries"]) if group else None,
            "by_trigger": by_trigger,
        }
    elif measurement:
        notes.append(
            "the receipts carry no boundary with both preTokens and postTokens, so "
            "the effective trigger is unknown here, not zero"
        )

    divergence = None
    if measured is None:
        verdict = "no_measurement"
    elif mixture is not None and threshold_tokens is not None:
        verdict = "mixed_triggers"
        notes.append(
            "the receipts carry more than one trigger value and none is %r, so the "
            "declared trigger is not compared against a mixture: %s"
            % (
                AUTO_TRIGGER,
                "; ".join(
                    "%s median %s over %d boundary(ies)"
                    % (label, _thousands(group["median_pre"]), group["boundaries"])
                    for label, group in sorted(mixture.items())
                ),
            )
        )
    elif measured["median_pre"] > window:
        verdict = "window_below_measured"
        notes.append(
            "the declared window is smaller than the median pre-compaction size, so "
            "the declared share is a share of a budget the client does not appear to "
            "be using; no trigger comparison is made"
        )
    elif threshold_tokens is None:
        verdict = "threshold_absent"
        notes.append(
            "no trigger was declared, so the measurement is printed and nothing is "
            "compared against it"
        )
    else:
        relative = (threshold_tokens - measured["median_pre"]) / measured["median_pre"]
        divergence = {
            "declared_tokens": threshold_tokens,
            "measured_tokens": measured["median_pre"],
            "relative": relative,
            "tolerance": tolerance,
        }
        verdict = (
            "threshold_confirmed"
            if abs(relative) <= tolerance
            else "threshold_diverges"
        )

    return {
        "mode": "ratio",
        "declared": {
            "per_turn": per_turn,
            "per_turn_source": per_turn_source,
            "window": window,
            "window_source": "flag",
        },
        "ratios": ratios,
        "measured": measured,
        "divergence": divergence,
        "tolerance": tolerance,
        "verdict": verdict,
        "notes": notes,
    }


def _thousands(value):
    return format(int(value), ",") if value is not None else "n/a"


def _pct1(value):
    return "n/a" if value is None else "%.1f%%" % (100.0 * value)


def render_ratio_markdown(payload):
    """Render the declared-vs-measured ratio without inventing either side."""
    declared = payload["declared"]
    ratios = payload["ratios"]
    lines = [
        "# ctxbudget — per-turn overhead against a declared window",
        "",
        "declared: %s per-turn tokens (%s) inside a %s-token window (`--window`)"
        % (
            _thousands(declared["per_turn"]),
            "from the project scan"
            if declared.get("per_turn_source") == "scan"
            else "`--per-turn`",
            _thousands(declared["window"]),
        ),
        "verdict: **%s**" % payload["verdict"],
        "",
        "## The ratio",
        "",
        "- %s / %s = **%s** of the window is re-injected every turn"
        % (
            _thousands(declared["per_turn"]),
            _thousands(declared["window"]),
            share_text(ratios["per_turn_over_window"]),
        ),
        "- %s turns of nothing but that overhead would consume the whole window"
        % _thousands(ratios["turns_to_window"]),
    ]
    if ratios["threshold_tokens"] is not None:
        lines.append(
            "- a trigger at %s of the window is %s tokens, which the overhead alone reaches in %s turns"
            % (
                _pct1(ratios["threshold_fraction"]),
                _thousands(ratios["threshold_tokens"]),
                _thousands(ratios["turns_to_threshold"]),
            )
        )

    lines.extend(["", "## Declared vs measured", ""])
    measured = payload.get("measured")
    if measured is None:
        lines.append(
            "- nothing was measured: no `--measure` directory was read, so no "
            "declared number is compared against a receipt"
        )
    else:
        lines.append(
            "- measured: median pre-compaction %s tokens over %s boundaries under `%s`"
            % (
                _thousands(measured["median_pre"]),
                _thousands(measured["boundaries"]),
                measured["root"],
            )
        )
        by_trigger = measured.get("by_trigger") or {}
        if by_trigger:
            others = [
                "`%s` %s over %s"
                % (label, _thousands(group["median_pre"]), _thousands(group["boundaries"]))
                for label, group in sorted(by_trigger.items())
                if label != measured.get("trigger")
            ]
            if measured.get("trigger") is not None:
                lines.append(
                    "- trigger group: `%s` — %s of %s boundary(ies); this is the population "
                    "the declared trigger is compared with"
                    % (
                        measured["trigger"],
                        _thousands(measured.get("group_boundaries")),
                        _thousands(measured["boundaries"]),
                    )
                )
                if others:
                    lines.append(
                        "- other triggers: %s — printed, not averaged into the comparison"
                        % "; ".join(others)
                    )
                else:
                    lines.append(
                        "- only one trigger value is recorded in these receipts, so this "
                        "grouping cannot move the median; a manual `/compact` would land in "
                        "its own group"
                    )
            else:
                lines.append(
                    "- trigger group: %s values and none is `%s`, so no declared trigger is "
                    "compared with a mixture — %s"
                    % (
                        len(by_trigger),
                        AUTO_TRIGGER,
                        "; ".join(others),
                    )
                )
        if payload["verdict"] == "window_below_measured":
            lines.append(
                "- the declared window (%s tokens) is below that median, so the "
                "declared share describes a budget the client is not using"
                % _thousands(declared["window"])
            )
        if payload.get("divergence"):
            divergence = payload["divergence"]
            lines.extend(
                [
                    "",
                    "| | tokens | basis |",
                    "|---|---:|---|",
                    "| declared trigger | %s | %s of the window you declared |"
                    % (
                        _thousands(divergence["declared_tokens"]),
                        _pct1(ratios["threshold_fraction"]),
                    ),
                    "| measured trigger | %s | median preTokens over %s boundaries |"
                    % (
                        _thousands(divergence["measured_tokens"]),
                        _thousands(measured["boundaries"]),
                    ),
                    "",
                    "- declared vs measured: %+.1f%% (tolerance %s)"
                    % (
                        100.0 * divergence["relative"],
                        _pct1(payload["tolerance"]),
                    ),
                ]
            )

    if payload.get("notes"):
        lines.extend(["", "Notes:"])
        for note in payload["notes"]:
            lines.append("- %s" % note)
    return "\n".join(lines)
