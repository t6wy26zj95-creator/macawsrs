"""Scheduling: FSRS (the scheduler Anki uses) plus which cards are due.

Card states follow Anki: 0 New, 1 Learning, 2 Review, 3 Relearning.
Claude only ever chooses a rating; due dates come from here.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from fsrs import Card, Rating, Scheduler, State

from .db import Store, iso, parse

NEW = 0
RATING_NAMES = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}
RATING_BY_NAME = {v.lower(): k for k, v in RATING_NAMES.items()}


def scheduler_for(user: Mapping[str, Any]) -> Scheduler:
    return Scheduler(desired_retention=float(user["desired_retention"]))


def schedule_of(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "state": row["state"],
        "step": row["step"],
        "stability": row["stability"],
        "difficulty": row["difficulty"],
        "due": row["due"],
        "last_review": row["last_review"],
    }


def _to_fsrs(card_id: int, sched: Mapping[str, Any]) -> Card:
    if sched["state"] == NEW:
        return Card(card_id=card_id, state=State.Learning, step=0)
    return Card(
        card_id=card_id,
        state=State(sched["state"]),
        step=sched["step"],
        stability=sched["stability"],
        difficulty=sched["difficulty"],
        due=parse(sched["due"]),
        last_review=parse(sched["last_review"]),
    )


def _from_fsrs(card: Card) -> dict[str, Any]:
    return {
        "state": int(card.state),
        "step": card.step,
        "stability": card.stability,
        "difficulty": card.difficulty,
        "due": iso(card.due),
        "last_review": iso(card.last_review),
    }


def next_schedule(
    scheduler: Scheduler, card_id: int, before: Mapping[str, Any], rating: int, when: datetime
) -> dict[str, Any]:
    card = _to_fsrs(card_id, before)
    new_card, _ = scheduler.review_card(card, Rating(rating), review_datetime=when.astimezone(timezone.utc))
    return _from_fsrs(new_card)


def grade(store: Store, user: Mapping[str, Any], card_id: int, rating: int, source: str,
          reason: str | None = None, when: datetime | None = None) -> int:
    """Apply a rating to a card and log it. Returns the review log id."""
    when = when or datetime.now(timezone.utc)
    row = store.card(card_id)
    if row is None:
        raise ValueError(f"card {card_id} not found")
    before = schedule_of(row)
    after = next_schedule(scheduler_for(user), card_id, before, rating, when)
    store.set_card_schedule(card_id, after)
    store.x("UPDATE cards SET buried_until=NULL WHERE id=?", (card_id,))
    return store.add_review(card_id, rating, when, before, after, source, reason)


def regrade(store: Store, user: Mapping[str, Any], log_id: int, rating: int) -> dict[str, Any]:
    """Change the rating of a past review, like Anki's undo + answer again.

    Only the latest review of a card can be changed; later reviews would be
    built on the old rating.
    """
    import json

    log = store.review(log_id)
    if log is None:
        raise ValueError("review not found")
    if store.last_review_id(log["card_id"]) != log_id:
        raise ValueError("this card has been reviewed again since")
    before = json.loads(log["before"])
    after = next_schedule(scheduler_for(user), log["card_id"], before, rating, parse(log["reviewed_at"]))
    store.set_card_schedule(log["card_id"], after)
    store.update_review(log_id, rating, after, "user")
    return after


# ---------- time helpers ----------


def tz_of(user: Mapping[str, Any]) -> tzinfo:
    try:
        return ZoneInfo(user["timezone"])
    except Exception:
        return timezone.utc


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h) % 24, int(m))


def is_quiet(user: Mapping[str, Any], now: datetime) -> bool:
    local = now.astimezone(tz_of(user)).time()
    start, end = _hm(user["quiet_start"]), _hm(user["quiet_end"])
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def day_start(user: Mapping[str, Any], now: datetime) -> datetime:
    """Start of the user's study day. The day rolls over when quiet hours end,
    like Anki's 'next day starts at' setting, so a session after midnight still
    counts as the same day."""
    tz = tz_of(user)
    local = now.astimezone(tz)
    rollover = _hm(user["quiet_end"])
    start = local.replace(hour=rollover.hour, minute=rollover.minute, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    return start


def awake_end(user: Mapping[str, Any], now: datetime) -> datetime:
    """When today's awake window ends (start of quiet hours)."""
    ds = day_start(user, now)
    qs, qe = _hm(user["quiet_start"]), _hm(user["quiet_end"])
    awake_minutes = ((qs.hour * 60 + qs.minute) - (qe.hour * 60 + qe.minute)) % (24 * 60)
    if awake_minutes == 0:
        awake_minutes = 24 * 60
    return ds + timedelta(minutes=awake_minutes)


def next_day_start(user: Mapping[str, Any], now: datetime) -> datetime:
    return day_start(user, now) + timedelta(days=1)


# ---------- what is due ----------


def due_queue(store: Store, user: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Cards to review now, in order: learning cards that are due, then review
    cards due today, then new cards within each deck's daily limit.
    Daily review limits are per deck, as in Anki."""
    ds = day_start(user, now)
    day_end = ds + timedelta(days=1)
    today = store.reviews_since(user["id"], ds)
    new_done: dict[int, int] = {}
    rev_done: dict[int, int] = {}
    for r in today:
        if r["before_state"] == NEW:
            new_done[r["deck_id"]] = new_done.get(r["deck_id"], 0) + 1
        else:
            rev_done[r["deck_id"]] = rev_done.get(r["deck_id"], 0) + 1

    learning, review, new = [], [], []
    for c in store.user_cards(user["id"]):
        buried = parse(c["buried_until"])
        if buried and buried > now:
            continue
        due = parse(c["due"])
        item = dict(c)
        if c["state"] == NEW:
            new.append(item)
        elif c["state"] in (1, 3):
            if due and due <= now:
                learning.append(item)
        elif due and due < day_end:
            review.append(item)

    learning.sort(key=lambda c: c["due"])
    review.sort(key=lambda c: c["due"])
    new.sort(key=lambda c: (c["created_at"], c["ord"]))

    out = list(learning)
    for c in review:
        d = c["deck_id"]
        if rev_done.get(d, 0) < c["reviews_per_day"]:
            rev_done[d] = rev_done.get(d, 0) + 1
            out.append(c)
    for c in new:
        d = c["deck_id"]
        if new_done.get(d, 0) < c["new_per_day"]:
            new_done[d] = new_done.get(d, 0) + 1
            out.append(c)
    return out


def coming_back_today(store: Store, user: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Cards in learning steps (new or forgotten) that come back later today, earliest first."""
    day_end = day_start(user, now) + timedelta(days=1)
    out = []
    for c in store.user_cards(user["id"]):
        buried = parse(c["buried_until"])
        due = parse(c["due"])
        if c["state"] in (1, 3) and due and now < due < day_end and not (buried and buried > now):
            out.append(dict(c))
    return sorted(out, key=lambda c: c["due"])


def due_today(store: Store, user: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Everything still to review today: due now, plus learning cards coming back later today."""
    return due_queue(store, user, now) + coming_back_today(store, user, now)


def upcoming(store: Store, user: Mapping[str, Any], now: datetime, days: int = 7) -> list[tuple[datetime, int]]:
    """Cards already scheduled for each of the next study days (after today), as
    (day start, count) for days with at least one card. New cards are not included."""
    first = next_day_start(user, now)
    counts: dict[int, int] = {}
    for c in store.user_cards(user["id"]):
        due = parse(c["due"])
        if c["state"] == NEW or due is None:
            continue
        buried = parse(c["buried_until"])
        if buried and buried > due:
            due = buried
        if due < first:
            continue
        i = int((due - first).total_seconds() // 86400)
        if i < days:
            counts[i] = counts.get(i, 0) + 1
    return [(first + timedelta(days=i), n) for i, n in sorted(counts.items())]


def comes_back(user: Mapping[str, Any], card: Mapping[str, Any], now: datetime) -> str:
    """When the user will actually see this card, in the same day rule the timer uses.

    Review cards count by study day (like Anki): anything due before the next
    rollover belongs to that study day, so "Fri 00:08" really means "from 08:00
    on Thursday". Learning cards come back at their exact time.
    """
    if card["state"] == NEW:
        return "not studied yet (comes up as a new card)"
    due = parse(card["due"])
    buried = parse(card["buried_until"])
    if buried and (due is None or buried > due):
        due = buried
    if due is None:
        return "unknown"
    tz = tz_of(user)
    if card["state"] in (1, 3) and not buried:
        if due <= now:
            return "now"
        when = due.astimezone(tz)
        if due < day_start(user, now) + timedelta(days=1):
            return f"today at {when:%H:%M}"
        return f"{when:%a %d %b} at {when:%H:%M}"
    shown = day_start(user, due)
    today = day_start(user, now)
    if shown <= today:
        return "today (it's due now)"
    start = shown.astimezone(tz).strftime("%H:%M")
    if shown == today + timedelta(days=1):
        return f"tomorrow ({shown.astimezone(tz):%a}) from {start}"
    return f"{shown.astimezone(tz):%a %d %b} from {start}"


def next_learning_due(store: Store, user: Mapping[str, Any], now: datetime) -> datetime | None:
    """Earliest future due time of a learning/relearning card (wrong answers coming back)."""
    times = [
        parse(c["due"])
        for c in store.user_cards(user["id"])
        if c["state"] in (1, 3) and c["due"] and parse(c["due"]) > now
    ]
    return min(times) if times else None


def describe_interval(due: datetime, now: datetime) -> str:
    def n(x: int, unit: str) -> str:
        return f"{x} {unit}" + ("" if x == 1 else "s")

    delta = due - now
    minutes = max(1, round(delta.total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes / 60
    if hours < 24:
        return n(round(hours), "hour")
    days = hours / 24
    if days < 60:
        return n(round(days), "day")
    if days < 365:
        return n(round(days / 30), "month")
    return f"{days / 365:.1f} years"
