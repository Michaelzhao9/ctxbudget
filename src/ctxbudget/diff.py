"""Compare two disk receipts: what grew, what shrank, by how much.

A single scan measures a state. Two scans measure a rate — which is what people
actually need when a store grows by hundreds of megabytes a day (claude-mem
#5057) and no product exposes a per-folder growth number.

Deliberate limits, so the numbers stay honest:

* **Only disk receipts are accepted.** A scan-mode payload measures context
  tokens, not bytes; mixing the two would produce a delta with no unit.
* **Different homes are refused.** Two receipts from two machines describe two
  different trees; subtracting them yields a plausible-looking lie.
* **A missing project is zero, not "unknown".** The receipt lists what existed
  at that moment, so absent means "did not hold bytes then". That is a real
  measurement, unlike a store that was never scanned.
* Sizes are apparent bytes from ``stat()``, the same unit as the receipts.
"""

import datetime
import json

_UNIT = 1024


def _parse_time(value):
    """Parse the receipt timestamp format, or return None if it is not one."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            continue
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def load_receipt(path):
    """Read a receipt written by ``--disk --receipt`` / ``--disk --json``.

    Raises ``ValueError`` for anything that is not a disk receipt: a wrong file
    is a setup mistake, and silently diffing it would invent numbers.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise ValueError("cannot read receipt %s: %s" % (path, exc))
    except ValueError as exc:
        raise ValueError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(payload, dict) or payload.get("tool") != "ctxbudget":
        raise ValueError("%s is not a ctxbudget receipt" % path)
    if payload.get("mode") != "disk":
        raise ValueError(
            "%s is a %r receipt; --diff compares --disk receipts only"
            % (path, payload.get("mode"))
        )
    if not isinstance(payload.get("stores"), list):
        raise ValueError("%s has no stores list" % path)
    return payload


def _store_bytes(payload):
    out = {}
    labels = {}
    for store in payload.get("stores") or []:
        if not isinstance(store, dict) or "id" not in store:
            continue
        out[store["id"]] = int(store.get("bytes") or 0)
        labels[store["id"]] = store.get("label") or store["id"]
    return out, labels


def _project_bytes(payload):
    out = {}
    for bucket in payload.get("projects") or []:
        if not isinstance(bucket, dict) or "key" not in bucket:
            continue
        ident = (bucket.get("store_id") or "", bucket["key"])
        out[ident] = int(bucket.get("bytes") or 0)
    return out


def _delta_rows(before_map, after_map, top=None, key_name="store_id"):
    """Union of both sides, growth first, ties broken by name for stability."""
    keys = set(before_map) | set(after_map)
    rows = []
    for key in keys:
        before = before_map.get(key, 0)
        after = after_map.get(key, 0)
        if key_name == "store_id":
            row = {"store_id": key, "before": before, "after": after, "delta": after - before}
        else:
            row = {
                "store_id": key[0],
                "key": key[1],
                "before": before,
                "after": after,
                "delta": after - before,
            }
        rows.append(row)
    rows.sort(key=lambda r: (-r["delta"], str(r.get("key") or r["store_id"])))
    total = len(rows)
    if top is not None and top >= 0:
        rows = rows[:top]
    return rows, total


def diff_receipts(old, new, top=10):
    """Return a diff payload for two disk receipts (old first, new second)."""
    before_home = old.get("home")
    after_home = new.get("home")
    if before_home != after_home:
        return {
            "tool": "ctxbudget",
            "mode": "diff",
            "status": "refused",
            "reason": (
                "refusing to diff receipts from different homes "
                "(%r vs %r): the byte deltas would compare two different trees"
                % (before_home, after_home)
            ),
            "from": {"home": before_home, "measured_at": old.get("measured_at")},
            "to": {"home": after_home, "measured_at": new.get("measured_at")},
        }

    before_total = int(old.get("total_bytes") or 0)
    after_total = int(new.get("total_bytes") or 0)
    before_stores, labels = _store_bytes(old)
    after_stores, after_labels = _store_bytes(new)
    # The newer receipt owns the current label for a store; it is the present tense.
    labels.update(after_labels)
    stores, _ = _delta_rows(before_stores, after_stores)

    payload = {
        "tool": "ctxbudget",
        "mode": "diff",
        "status": "ok",
        "home": before_home,
        "from": {
            "measured_at": old.get("measured_at"),
            "total_bytes": before_total,
            "files": old.get("total_files"),
        },
        "to": {
            "measured_at": new.get("measured_at"),
            "total_bytes": after_total,
            "files": new.get("total_files"),
        },
        "span_hours": None,
        "total_before": before_total,
        "total_after": after_total,
        "total_delta": after_total - before_total,
        "stores": [
            dict(row, label=labels.get(row["store_id"], row["store_id"]))
            for row in stores
        ],
        "notes": [
            "apparent bytes from stat(), same unit as the receipts",
            "a store or directory absent from one receipt counts as 0 bytes then",
            "growth only: this never claims what a file is used for",
        ],
        "home_verified": True,
    }

    if old.get("redacted") or new.get("redacted"):
        # Redaction rewrites both homes to ``~``, so the equality check above
        # proves nothing. Say that instead of implying the machine was verified.
        payload["home_verified"] = False
        payload["notes"].append(
            "at least one receipt is redacted, so both homes read as `~` and the "
            "same-machine check could not run: confirm both scans came from the "
            "same tree, or re-export without --redact to compare across machines"
        )

    started = _parse_time(old.get("measured_at"))
    ended = _parse_time(new.get("measured_at"))
    if started and ended:
        payload["span_hours"] = round((ended - started).total_seconds() / 3600.0, 3)

    before_projects = _project_bytes(old)
    after_projects = _project_bytes(new)
    if before_projects or after_projects:
        if "projects" in old and "projects" in new:
            rows, total = _delta_rows(before_projects, after_projects, top=top, key_name="key")
            for row in rows:
                row["label"] = labels.get(row["store_id"], row["store_id"])
            payload["projects_available"] = True
            payload["projects_total"] = total
            payload["projects"] = rows
        else:
            payload["projects_available"] = False
            payload["projects_note"] = (
                "one receipt lists no projects (re-run that scan with "
                "--by-project N) so directory-level deltas are not available"
            )
    else:
        payload["projects_available"] = False

    return payload

