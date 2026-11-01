"""Deletion schedules: how long the bytes in a store are actually kept.

An on-disk audit answers "how big is this?" without answering "how long is this
true?". Claude Code deletes transcripts older than ``cleanupPeriodDays``
(default 30) at startup; the setting is absent from a default
``~/.claude/settings.json`` and is not exposed in the app's settings UI
(claude-code #105500, #105498), so a reader of the size report has no way to
know the number expires.

Three rules keep this honest:

1. **Read, never assume.** A schedule is reported only when it was read from a
   named config file (``witness="config"``) or when the product documents a
   default and no config overrides it (``witness="product-default"``). A store
   with no known schedule reports ``witness="unknown"`` and ``days=None``.
2. **Unknown is not zero.** A store whose schedule is unknown makes no expiry
   claim at all; it is counted in ``stores_unknown`` instead.
3. **The store can falsify the config.** Files older than the reported window
   that still exist mean the schedule did not run (or does not use file age).
   That is reported as ``schedule_not_in_effect`` rather than smoothed away,
   because it is the one claim here that is measured rather than read.

Read-only: only ``stat()`` and small JSON reads. No file contents of stores are
read, no network, nothing is written.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

DAY = 86400.0

#: Documented deletion schedules, keyed by store id.
#:
#: ``default_days`` and ``ui_exposed`` are static annotations, not measurements:
#: they describe what the product documents, and each carries ``basis`` so a
#: reader can check the claim instead of trusting it. ``config_relative`` is the
#: file the setting is read from, relative to the audited home.
RETENTION_RULES: Dict[str, dict] = {
    "claude-projects": {
        "setting": "cleanupPeriodDays",
        "config_relative": ".claude/settings.json",
        "config_format": "json",
        "default_days": 30,
        "ui_exposed": False,
        "basis": (
            "cleanupPeriodDays deletes at startup regardless of the archive "
            "setting, and the app exposes no field for it (claude-code #105500)"
        ),
    },
    # codex-sessions / codex-archived: no day-based deletion setting is
    # documented for codex's session rollouts; searched ~/.codex/config.toml for
    # keys mentioning history/retention/cleanup/days and found none. Recorded as
    # unknown rather than assumed to be infinite.
}


@dataclass
class RetentionPlan:
    store_id: str
    label: str
    exists: bool
    setting: Optional[str] = None
    config_path: Optional[str] = None
    configured: bool = False
    days: Optional[int] = None
    witness: str = "unknown"
    ui_exposed: Optional[bool] = None
    basis: str = ""
    files_expired: int = 0
    bytes_expired: int = 0
    shorter_than_window: bool = False
    verdict: str = "unknown_schedule"


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _configured_days(rule, home):
    """Return (days, configured, config_path) for one rule, or (None, False, None)."""
    relative = rule.get("config_relative")
    if not relative or rule.get("config_format") != "json":
        return None, False, None
    path = os.path.join(home, relative)
    if not os.path.isfile(path):
        return None, False, None
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return None, False, path
    value = payload.get(rule["setting"])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, False, path
    if value <= 0:
        # A non-positive window is what the product would reject; do not invent
        # a schedule from it.
        return None, False, path
    return int(value), True, path


def _file_ages(paths):
    """Yield (bytes, age_seconds) for every regular file, without following links."""
    for root in paths:
        if os.path.isfile(root):
            if os.path.islink(root):
                continue
            try:
                stat = os.stat(root)
            except OSError:
                continue
            yield stat.st_size, max(0.0, time.time() - stat.st_mtime)
            continue
        for current, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if not os.path.islink(os.path.join(current, name))
            ]
            for name in names:
                full = os.path.join(current, name)
                try:
                    if os.path.islink(full):
                        continue
                    stat = os.stat(full)
                except OSError:
                    continue
                yield stat.st_size, max(0.0, time.time() - stat.st_mtime)


def plan_retention(stores, home, window_days=30) -> Tuple[List[RetentionPlan], dict]:
    """Attach a deletion schedule to each store and measure what is past it.

    ``window_days`` is the period the report claims to describe; a store whose
    schedule is shorter than the window cannot be compared across it.
    """
    home = os.path.abspath(os.path.expanduser(str(home)))
    plans: List[RetentionPlan] = []
    measured = known = unknown = 0
    files_expired = bytes_expired = 0
    for store in stores:
        rule = RETENTION_RULES.get(store.id)
        plan = RetentionPlan(store_id=store.id, label=store.label, exists=store.exists)
        if rule:
            plan.setting = rule["setting"]
            plan.ui_exposed = rule["ui_exposed"]
            plan.basis = rule["basis"]
            days, configured, config_path = _configured_days(rule, home)
            plan.config_path = config_path
            plan.configured = configured
            if days is not None:
                plan.days = days
                plan.witness = "config"
            elif rule.get("default_days"):
                plan.days = rule["default_days"]
                plan.witness = "product-default"
        if store.exists and store.paths:
            measured += 1
            if plan.days is None:
                unknown += 1
            else:
                known += 1
                limit = plan.days * DAY
                for size, age in _file_ages(store.paths):
                    if age > limit:
                        plan.files_expired += 1
                        plan.bytes_expired += size
                files_expired += plan.files_expired
                bytes_expired += plan.bytes_expired
                plan.shorter_than_window = plan.days < window_days
                plan.verdict = (
                    "schedule_not_in_effect"
                    if plan.files_expired
                    else "consistent"
                )
        plans.append(plan)
    stats = {
        "window_days": window_days,
        "stores_measured": measured,
        "stores_known": known,
        "stores_unknown": unknown,
        "files_expired": files_expired,
        "bytes_expired": bytes_expired,
    }
    return plans, stats
