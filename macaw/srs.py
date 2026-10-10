"""Scheduling: FSRS (the scheduler Anki uses) plus which cards are due.

Card states follow Anki: 0 New, 1 Learning, 2 Review, 3 Relearning.
Claude only ever chooses a rating; due dates come from here.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import json

from fsrs import Card, Rating, Scheduler, State

from . import pacing
from .db import Store, iso, parse

NEW = 0
REVIEW = 2  # 1 and 3 are (re)learning steps
LONG_OVERDUE = timedelta(days=30)
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
          reason: str | None = None, when: datetime | None = None, missed: str | None = None) -> int:
    """Apply a rating to a card and log it. Returns the review log id."""
    when = when or datetime.now(timezone.utc)
    row = store.card(card_id)
    if row is None:
        raise ValueError(f"card {card_id} not found")
    before = schedule_of(row)
    after = next_schedule(scheduler_for(user), card_id, before, rating, when)
    store.set_card_schedule(card_id, after)
    store.x("UPDATE cards SET buried_until=NULL WHERE id=?", (card_id,))
    return store.add_review(card_id, rating, when, before, after, source, reason, missed)


def regrade(store: Store, user: Mapping[str, Any], log_id: int, rating: int,
            source: str = "user", reason: str | None = None) -> dict[str, Any]:
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
    store.update_review(log_id, rating, after, source, reason)
    return after


# ---------- time helpers ----------


def recall_at(scheduler: Scheduler, sched: Mapping[str, Any], when: datetime) -> float:
    """The chance, as the scheduler sees it, that a studied card is still remembered at `when`."""
    if sched.get("stability") is None or sched.get("last_review") is None:
        return 1.0
    card = _to_fsrs(0, sched)
    return scheduler.get_card_retrievability(card, current_datetime=when.astimezone(timezone.utc))


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


def study_days_between(user: Mapping[str, Any], earlier: datetime, later: datetime) -> int:
    """How many study days later `later` is: 0 on the same study day, 1 on the next."""
    return (day_start(user, later).date() - day_start(user, earlier).date()).days


# ---------- what is due ----------


def _left_over(card: Mapping[str, Any], ds: datetime, now: datetime) -> bool:
    """A studied card that was due on an earlier study day and still hasn't been done."""
    if card["state"] == NEW:
        return False
    buried = parse(card["buried_until"])
    if buried and buried > now:
        return False
    due = parse(card["due"])
    return due is not None and due < ds


def backlog(store: Store, user: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """How far behind the user is, from the cards and the review log:
    overdue = studied cards left over from earlier study days,
    overdue_done = left-over cards already done today,
    pace = answers a day over the last (up to) 7 study days,
    again_rate = share of real reviews (cards out of their learning steps) answered Again
    in that time. Misses in learning steps don't count: a word just met is often missed.
    expected_again = the share of those reviews the scheduler itself expected to be forgotten,
    given how long each card had waited (a card a year overdue is expected to be forgotten often),
    long_overdue = how many of those reviews were of cards more than LONG_OVERDUE past due."""
    ds = day_start(user, now)
    overdue = sum(1 for c in store.user_cards(user["id"]) if _left_over(c, ds, now))
    week = store.reviews_since(user["id"], ds - timedelta(days=7))
    overdue_done, before_today, studied, again = 0, [], 0, 0
    expected, long_overdue = 0.0, 0
    scheduler = scheduler_for(user)
    for r in week:
        at = parse(r["reviewed_at"])
        if at > now:
            continue
        before = json.loads(r["before"])
        if at >= ds:
            due = parse(before.get("due"))
            if before.get("state") != NEW and due is not None and due < ds:
                overdue_done += 1
        else:
            before_today.append(at)
        if before.get("state") == REVIEW:
            studied += 1
            again += r["rating"] == 1
            expected += 1 - recall_at(scheduler, before, at)
            due = parse(before.get("due"))
            long_overdue += due is not None and at - due > LONG_OVERDUE
    pace = 0.0
    if before_today:
        days = max(1, min(7, study_days_between(user, min(before_today), now)))
        pace = len(before_today) / days
    return {
        "overdue": overdue,
        "overdue_done": overdue_done,
        "pace": pace,
        "again_rate": again / studied if studied else None,
        "studied_reviews": studied,
        "expected_again": expected / studied if studied else None,
        "long_overdue": long_overdue,
    }


def status_of(b: Mapping[str, Any]) -> str:
    return pacing.backlog_status(b["overdue"], b["pace"])


def due_queue(store: Store, user: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Cards to review now, in order: learning cards that are due, then review
    cards due today, then new cards within each deck's daily limit.
    Daily review limits are per deck, as in Anki. While there is a backlog, fewer
    new cards come in (see pacing.new_card_cap), so it doesn't keep growing."""
    ds = day_start(user, now)
    day_end = ds + timedelta(days=1)
    b = backlog(store, user, now)
    status = status_of(b)
    hard = pacing.material_is_hard(b["again_rate"], b["studied_reviews"])
    today = store.reviews_since(user["id"], ds)
    reviews_done = sum(1 for r in today if r["before_state"] == REVIEW)
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
    plan = store.day_plan(user["id"], ds.date().isoformat())
    if plan is not None:
        # The bot set today's plan: its number of new cards, within each deck's own limit.
        room = plan["new_cards"] - sum(new_done.values())

        def cap(c: Mapping[str, Any]) -> int:
            return c["new_per_day"]
    else:
        # No plan yet: new cards only while today's reviews leave room for them (all decks
        # together), and fewer while there's a backlog.
        room = pacing.new_card_room(reviews_done + len(review), b["pace"]) - sum(new_done.values())

        def cap(c: Mapping[str, Any]) -> int:
            return pacing.new_card_cap(c["new_per_day"], status, hard)
    for c in new:
        d = c["deck_id"]
        if room > 0 and new_done.get(d, 0) < cap(c):
            new_done[d] = new_done.get(d, 0) + 1
            room -= 1
            out.append(c)
    return out


def new_card_limits(store: Store, user: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Today's new cards, all decks together: the decks' own limits, what the plan allows,
    and why it allows fewer (backlog, material, workload)."""
    ds = day_start(user, now)
    b = backlog(store, user, now)
    status = status_of(b)
    hard = pacing.material_is_hard(b["again_rate"], b["studied_reviews"])
    limit = capped = 0
    for d in store.decks(user["id"]):
        limit += d["new_per_day"]
        capped += pacing.new_card_cap(d["new_per_day"], status, hard)
    today = store.reviews_since(user["id"], ds)
    load = sum(1 for r in today if r["before_state"] == REVIEW) + sum(
        1 for c in store.user_cards(user["id"])
        if c["state"] == REVIEW and parse(c["due"]) and parse(c["due"]) < ds + timedelta(days=1)
        and not (parse(c["buried_until"]) and parse(c["buried_until"]) > now)
    )
    room = pacing.new_card_room(load, b["pace"])
    # A limit the deck can't reach anyway (few new cards left) isn't held back by the plan.
    new_done = sum(1 for r in today if r["before_state"] == NEW)
    waiting = sum(1 for c in store.user_cards(user["id"]) if c["state"] == NEW)
    limit = min(limit, new_done + waiting)
    capped = min(capped, limit)
    reasons = []
    if status != "on_track" and pacing.new_card_cap(limit, status) < limit:
        reasons.append("backlog")
    if hard and capped < limit:
        reasons.append("hard")
    if room < capped:
        reasons.append("workload")
    return {"limit": limit, "allowed": min(capped, room), "reasons": reasons, "review_load": load,
            "capacity": round(pacing.daily_capacity(b["pace"]))}


def study_day_starts(user: Mapping[str, Any], now: datetime, days: int) -> list[datetime]:
    """Starts of the last `days` study days before today, oldest first."""
    ds = day_start(user, now)
    # Step back from midday, so a daylight saving change can't skip or repeat a day.
    return [day_start(user, ds - timedelta(days=k) + timedelta(hours=12)) for k in range(days, 0, -1)]


def snapshot_day(store: Store, user: Mapping[str, Any], now: datetime) -> None:
    """Record what today looked like when it began (once a day), for the progress report.
    Taken later in the day, cards already done today are counted back in."""
    ds = day_start(user, now)
    day_end = ds + timedelta(days=1)
    b = backlog(store, user, now)
    remaining = 0
    for c in store.user_cards(user["id"]):
        buried = parse(c["buried_until"])
        due = parse(c["due"])
        if c["state"] != NEW and due is not None and due < day_end and not (buried and buried > now):
            remaining += 1
    done = {r["card_id"] for r in store.reviews_since(user["id"], ds)
            if r["before_state"] not in (None, NEW) and parse(r["reviewed_at"]) <= now}
    store.record_day_stats(user["id"], ds.date().isoformat(), remaining + len(done),
                           b["overdue"] + b["overdue_done"])


def history(store: Store, user: Mapping[str, Any], now: datetime, days: int = 7) -> list[dict[str, Any]]:
    """The last `days` study days before today, oldest first: answers given, different
    cards, new cards started, studied cards forgotten (Again), and, for days the bot
    recorded, how many were due at the start and how many of them were left over.
    left_after is what was still left at the end (the next day's left-over count)."""
    starts = study_day_starts(user, now, days)
    today = day_start(user, now)
    rows = [r for r in store.reviews_since(user["id"], starts[0]) if parse(r["reviewed_at"]) < today]
    stats = store.day_stats(user["id"], starts[0].date().isoformat())
    out = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else today
        day = [r for r in rows if start <= parse(r["reviewed_at"]) < end]
        studied = [r for r in day if r["before_state"] == REVIEW]
        st = stats.get(start.date().isoformat())
        after = stats.get(end.date().isoformat())
        out.append({
            "day": start,
            "answers": len(day),
            "cards": len({r["card_id"] for r in day}),
            "new": sum(1 for r in day if r["before_state"] == NEW),
            "forgotten": sum(1 for r in studied if r["rating"] == 1),
            "studied": len(studied),
            "due": st["due"] if st else None,
            "left_over": st["left_over"] if st else None,
            "left_after": after["left_over"] if after else None,
        })
    return out


def done_today(store: Store, user: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """What the user did this study day so far: every graded answer (a card asked
    twice counts twice), distinct cards, first-time cards, the grades, and the
    notes added in chat. Computed here so the bot can give exact numbers."""
    ds = day_start(user, now)
    rows = [r for r in store.reviews_since(user["id"], ds) if parse(r["reviewed_at"]) <= now]
    rows.sort(key=lambda r: r["id"])
    grades = {name: 0 for name in RATING_NAMES.values()}
    card_ids: list[int] = []
    for r in rows:
        grades[RATING_NAMES[r["rating"]]] += 1
        if r["card_id"] not in card_ids:
            card_ids.append(r["card_id"])
    return {
        "since": ds,
        "reviews": len(rows),
        "card_ids": card_ids,
        "first_time": sum(1 for r in rows if r["before_state"] == NEW),
        "grades": grades,
        "added": store.notes_added_between(user["id"], ds, now + timedelta(seconds=1)),
    }


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


def not_reviewed_here(store: Store, user: Mapping[str, Any]) -> int:
    """Studied cards (from an Anki import) that haven't had a review in the bot yet."""
    seen = {r["card_id"] for r in store.reviews_since(user["id"], datetime(1970, 1, 1, tzinfo=timezone.utc))}
    return sum(1 for c in store.user_cards(user["id"]) if c["state"] != NEW and c["id"] not in seen)


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


# ---------- deck stats ----------

MATURE_DAYS = 21


def empty_stats() -> dict[str, int]:
    return {"new": 0, "learning": 0, "young": 0, "mature": 0, "due": 0}


def deck_stats(store: Store, user: Mapping[str, Any], now: datetime) -> dict[int, dict[str, int]]:
    """Per deck, how many cards are new, learning (or relearning), young (interval under
    21 days), mature (21 days or more, as in Anki), and still due today."""
    out: dict[int, dict[str, int]] = {}
    for c in store.user_cards(user["id"]):
        s = out.setdefault(c["deck_id"], empty_stats())
        if c["state"] == NEW:
            s["new"] += 1
        elif c["state"] in (1, 3):
            s["learning"] += 1
        else:
            due, last = parse(c["due"]), parse(c["last_review"])
            ivl = (due - last).days if due and last else 0
            s["mature" if ivl >= MATURE_DAYS else "young"] += 1
    for c in due_today(store, user, now):
        out.setdefault(c["deck_id"], empty_stats())["due"] += 1
    return out
