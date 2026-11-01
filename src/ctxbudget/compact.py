"""What did compaction do to the window your config has to fit in?

``ctxbudget`` prices what the stack injects **per turn**. It never said how big
the window is that the injection has to fit inside, because that number is not a
configuration value — it is what the harness left after its last compaction.

Claude Code writes a receipt for every compaction into the session transcript
(``compactMetadata`` on a ``system`` record) with ``preTokens``, ``postTokens``,
``trigger`` and sometimes ``cumulativeDroppedTokens``. Nothing in the product's
own surfaces reads those fields back, so a user sees "context compacted" and no
arithmetic. This module reads the receipts and states three things per boundary:
what was there, what survived, and what the harness says it dropped.

Rules this module must keep:

1. **A ratio needs both of its numbers.** ``survived`` is ``post / pre`` and is
   ``None`` whenever either side is absent — never 0.0, never 1.0.
2. **An absent field is not a zero.** ``cumulativeDroppedTokens`` is missing on
   some boundaries; the sum covers the boundaries that carried it and reports
   how many did and how many did not.
3. **A negative result states what it covered.** Zero boundaries prints the file
   count it walked and refuses every ratio instead of printing ``0%``.

Read-only: transcripts are streamed line by line, only numeric compaction fields
are kept, and no message text is read into memory or written anywhere.
"""

import json
import os
import statistics
from typing import List

MAX_FILES = 5000


def _number(value):
    """Return an int/float token count, or None when the value is not a count."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _whole(value):
    """Round a token median to a whole token; a window is not 5000.5 tokens."""
    return int(round(value))


def _iter_boundaries(path):
    """Yield (record, metadata) for each compaction receipt in one transcript."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "compactMetadata" not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            meta = record.get("compactMetadata")
            if isinstance(meta, dict):
                yield record, meta


def scan_compactions(root, max_files=MAX_FILES) -> dict:
    """Read compaction receipts under *root* and price what survived them."""
    root = os.path.abspath(os.path.expanduser(str(root)))
    rows: List[dict] = []
    files_scanned = 0
    files_with_boundaries = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            if files_scanned >= max_files:
                break
            full = os.path.join(current, name)
            if os.path.islink(full):
                continue
            files_scanned += 1
            found_here = 0
            try:
                for _record, meta in _iter_boundaries(full):
                    found_here += 1
                    pre = _number(meta.get("preTokens"))
                    post = _number(meta.get("postTokens"))
                    survived = None
                    if pre is not None and post is not None and pre > 0:
                        survived = post / pre
                    rows.append(
                        {
                            "file": os.path.relpath(full, root).replace(os.sep, "/"),
                            "pre": pre,
                            "post": post,
                            "dropped": _number(meta.get("cumulativeDroppedTokens")),
                            "trigger": meta.get("trigger"),
                            "survived": survived,
                        }
                    )
            except OSError:
                continue
            if found_here:
                files_with_boundaries += 1

    with_both = [r for r in rows if r["survived"] is not None]
    drops = [r["dropped"] for r in rows if r["dropped"] is not None]
    survived_values = [r["survived"] for r in with_both]
    post_values = [r["post"] for r in with_both]
    post_pairs_pre = [r["pre"] for r in with_both]
    notes: List[str] = []

    if not rows:
        notes.append(
            "no compaction receipts under %d file(s) scanned: every ratio is "
            "refused here, which is a coverage statement, not a 0%% result" % files_scanned
        )

    payload = {
        "mode": "postcompact",
        "root": root,
        "coverage": {
            "files_scanned": files_scanned,
            "files_with_boundaries": files_with_boundaries,
        },
        "boundaries": rows,
        "totals": {
            "boundaries": len(rows),
            "with_pre_and_post": len(with_both),
            "dropped_sum": sum(drops) if drops else None,
            "dropped_present": len(drops),
            "dropped_missing": len(rows) - len(drops),
        },
        "survival": {
            "median": statistics.median(survived_values) if survived_values else None,
            "min": min(survived_values) if survived_values else None,
            "max": max(survived_values) if survived_values else None,
            "n": len(survived_values),
        },
        "window": {
            "median_pre": _whole(statistics.median(post_pairs_pre)) if post_pairs_pre else None,
            "median_post": _whole(statistics.median(post_values)) if post_values else None,
            "min_post": min(post_values) if post_values else None,
            "max_post": max(post_values) if post_values else None,
            "basis": "median of the surviving window, rounded to a whole token",
        },
        "verdict": (
            "no_compactions"
            if not rows
            else ("measured" if with_both else "no_measurable_boundaries")
        ),
        "turn_cost": None,
        "notes": notes,
    }
    return payload


def attach_turn_cost(payload, per_turn):
    """State the per-turn cost as a share of the surviving window.

    The denominator is the median post-compaction window, because a single
    boundary is an anecdote and the minimum would make every stack look bad.
    Both numbers go into the payload so no caller has to re-derive the share,
    and a receipt with no measurable boundary refuses instead of dividing by an
    absent number.
    """
    median_post = payload.get("window", {}).get("median_post")
    if per_turn is None:
        return payload
    if not median_post:
        payload["notes"].append(
            "turn cost not computed: no measurable window to divide by, so the "
            "share is refused rather than assumed"
        )
        return payload
    payload["turn_cost"] = {
        "per_turn": per_turn,
        "window": median_post,
        "window_basis": "median post-compaction tokens",
        "share": per_turn / median_post,
    }
    return payload
