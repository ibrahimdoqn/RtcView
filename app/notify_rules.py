"""Group notification schedule engine — drives each group's notify_enabled
switch from its notify_schedule rules.

Extracted out of the old ONVIF detection module (which owned it only
because it happened to be the thing ticking every few seconds) so it has
no dependency on whichever detection backend (ONVIF PullPoint, Home
Assistant, ...) is in use — HAManager and anything else that needs
"is this group currently allowed to notify" call into this module
directly instead.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("notify_rules")


def _last_rule_occurrence(rules: list, now: Optional[datetime] = None):
    """Most recent notification-rule occurrence at or before ``now``.

    Each rule is ``{"days": [0..6], "time": "HH:MM", "action": "on"|"off"}``
    and means "at this time, on these days, flip the group's notification
    switch". An empty ``days`` list means every day.

    Returns ``(action, when)`` for the latest occurrence within the past
    week, or ``None`` when there are no usable rules.

    Deliberately NOT the window model used by record_schedule
    ({days, start, end}): windows force you to describe a period, which
    gets awkward the moment one crosses midnight — "20:00-09:00 Mon-Sat
    plus 00:00-23:59 Sun" was really just trying to say "on at 20:00, off
    at 09:00". Actions say that directly.

    Two rules landing on the same minute resolve to "off", so the outcome
    never depends on list order.
    """
    if not rules:
        return None
    now = now or datetime.now()
    best = None                # (occurrence datetime, action)
    for r in rules:
        try:
            hh, mm = (int(x) for x in str(r.get("time", "")).split(":", 1))
        except (TypeError, ValueError):
            continue           # malformed row — ignore rather than crash
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            continue
        action = "off" if str(r.get("action", "on")).lower() == "off" else "on"
        days = r.get("days") or list(range(7))
        for d in days:
            try:
                d = int(d)
            except (TypeError, ValueError):
                continue
            if not 0 <= d <= 6:
                continue
            # Most recent occurrence of this weekday at this clock time.
            back = (now.weekday() - d) % 7
            occ = (now - timedelta(days=back)).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
            if occ > now:                      # today's time hasn't come yet
                occ -= timedelta(days=7)
            if best is None or occ > best[0] or (occ == best[0] and action == "off"):
                best = (occ, action)
    if best is None:
        return None
    return best[1], best[0]


def apply_notify_rules(config_store, now: Optional[datetime] = None) -> int:
    """Let the schedule drive the switch.

    The rules do not sit alongside the group's notification toggle as a
    second gate — they OPERATE it. When a rule's moment passes, this
    writes the new value into notify_enabled, so the switch the user sees
    in the sidebar physically moves and remains the single thing that
    decides whether notifications are delivered.

    A manual flip is therefore never fought: notify_rule_applied_at
    records which rule occurrence was last acted on, and only an
    occurrence NEWER than that is applied. Flip the switch by hand at
    10:30 and it stays flipped until the next rule comes round.

    That same marker makes downtime self-correcting: a boundary missed
    while the service was stopped is still newer than the stored marker,
    so the correct state is applied on the next tick after startup.

    Returns the number of groups whose stored state changed.
    """
    changed = 0
    for g in list(config_store.get_groups()):
        occ = _last_rule_occurrence(g.get("notify_schedule") or [], now)
        if occ is None:
            continue
        action, when = occ
        when_ts = when.timestamp()
        if when_ts <= float(g.get("notify_rule_applied_at", 0) or 0):
            continue                            # already acted on this one
        wanted = (action == "on")
        updates = {"notify_rule_applied_at": when_ts}
        if bool(g.get("notify_enabled", True)) != wanted:
            updates["notify_enabled"] = wanted
            log.info("Grup '%s': %s kuralı uygulandı, bildirimler %s",
                     g.get("name", g.get("id")), when.strftime("%a %H:%M"),
                     "açıldı" if wanted else "kapatıldı")
            changed += 1
        config_store.update_group(g["id"], updates)
    return changed


def group_notify_active(group: dict) -> bool:
    """Whether this group currently delivers notifications.

    Just the switch. The schedule already had its say by moving that
    switch (see apply_notify_rules), so there is exactly one place the
    answer can come from — no second gate that could disagree with what
    the UI is showing.
    """
    return bool(group.get("notify_enabled", True))
