"""When to bring up the next card, and when to send a reminder.

Pure functions over plain values so they are easy to test.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

MIN_GAP = timedelta(minutes=5)
MAX_GAP = timedelta(hours=2)
# The user counts as "here" for this long after their last message, so the bot
# keeps going at night if they chose to study then.
ACTIVE_WINDOW = timedelta(minutes=20)
# Don't interrupt a conversation that is in full swing with a new card.
CHAT_COOLDOWN = timedelta(seconds=90)


def gap_until_next_session(now: datetime, awake_end: datetime, due_count: int, cards_per_session: int) -> timedelta:
    """Spread the remaining sessions evenly over what is left of the awake day."""
    if due_count <= 0:
        return MAX_GAP
    sessions_left = math.ceil(due_count / max(1, cards_per_session))
    remaining = awake_end - now
    if remaining <= timedelta(0):
        return MIN_GAP
    gap = remaining / (sessions_left + 1)
    return max(MIN_GAP, min(MAX_GAP, gap))


def user_is_active(now: datetime, last_user_at: datetime | None) -> bool:
    return last_user_at is not None and now - last_user_at <= ACTIVE_WINDOW


def may_initiate(now: datetime, quiet: bool, last_user_at: datetime | None) -> bool:
    """The bot may start talking if it isn't quiet hours, or the user is studying right now."""
    return not quiet or user_is_active(now, last_user_at)


def should_ask(
    now: datetime,
    *,
    has_due: bool,
    active_card: bool,
    next_ask_at: datetime | None,
    quiet: bool,
    last_user_at: datetime | None,
    last_bot_at: datetime | None,
) -> bool:
    if not has_due or active_card:
        return False
    if not may_initiate(now, quiet, last_user_at):
        return False
    if next_ask_at is not None and now < next_ask_at:
        return False
    # Let a live exchange breathe before cutting in.
    latest = max([t for t in (last_user_at, last_bot_at) if t is not None], default=None)
    if latest is not None and now - latest < CHAT_COOLDOWN:
        return False
    return True


def next_reminder_at(
    *,
    last_user_at: datetime | None,
    asked_at: datetime,
    streak: int,
    first_gap_min: int,
    last_reminder_at: datetime | None = None,
) -> datetime:
    """Reminders double their gap: 1h, 2h, 4h, ... after the question (or the last reply)."""
    base = max([t for t in (asked_at, last_user_at) if t is not None])
    # Count each gap from the reminder actually sent, so reminders held back by
    # quiet hours don't all come due at once when the morning starts.
    if streak > 0 and last_reminder_at is not None and last_reminder_at > base:
        return last_reminder_at + timedelta(minutes=first_gap_min * (2**streak))
    total = sum(first_gap_min * (2**i) for i in range(streak + 1))
    return base + timedelta(minutes=total)


def should_remind(
    now: datetime,
    *,
    active_card: bool,
    asked_at: datetime | None,
    last_user_at: datetime | None,
    quiet: bool,
    reminders_today: int,
    max_reminders: int,
    streak: int,
    first_gap_min: int,
    last_reminder_at: datetime | None = None,
) -> bool:
    if not active_card or asked_at is None:
        return False
    # The gap counts from the later of the question and the user's last message,
    # so someone who is chatting is never reminded.
    if quiet or reminders_today >= max_reminders:
        return False
    return now >= next_reminder_at(
        last_user_at=last_user_at, asked_at=asked_at, streak=streak, first_gap_min=first_gap_min,
        last_reminder_at=last_reminder_at,
    )


def annoyance_level(streak: int) -> str:
    return ["friendly", "nudging", "playfully annoyed", "dramatically offended"][min(streak, 3)]


# ---------- a card left unanswered for days ----------
# The day the card is asked keeps the normal reminders. From the next study day on the
# bot writes at most once a day, at a different time each day, and skips some days.


def nag_skip_chance(days_ignored: int) -> float:
    """Skipping one day is fine, so the first days never skip; later ones skip more,
    like someone who knows they're being ignored but can't quite let go."""
    if days_ignored <= 2:
        return 0.0
    if days_ignored <= 6:
        return 0.25
    if days_ignored <= 20:
        return 0.35
    return 0.5


def _roll(user_id: int, day: date, what: str) -> float:
    # Seeded by user and day, so every tick of the same day makes the same choice.
    return random.Random(f"{user_id}:{day.isoformat()}:{what}").random()


def nag_skips_day(user_id: int, day: date, days_ignored: int) -> bool:
    def raw(d: date, n: int) -> bool:
        return _roll(user_id, d, "skip") < nag_skip_chance(n)

    if not raw(day, days_ignored):
        return False
    # Never three silent days in a row.
    one, two = day - timedelta(days=1), day - timedelta(days=2)
    return not (raw(one, days_ignored - 1) and raw(two, days_ignored - 2))


def nag_time(user_id: int, day_start: datetime, awake_end: datetime) -> datetime:
    """A different time every day: from an hour after the day starts to three hours before quiet hours."""
    awake = (awake_end - day_start) / timedelta(minutes=1)
    span = max(0.0, awake - 4 * 60)
    return day_start + timedelta(minutes=60 + _roll(user_id, day_start.date(), "time") * span)


def should_nag(
    now: datetime,
    *,
    user_id: int,
    day_start: datetime,
    awake_end: datetime,
    days_ignored: int,
    quiet: bool,
    nagged_today: bool,
    user_wrote_today: bool,
    check_in: bool = False,
) -> bool:
    """The one daily message about a card left unanswered for a day or more.
    A check-in after time the user asked for is never skipped."""
    if quiet or nagged_today or now >= awake_end:
        return False
    if now < nag_time(user_id, day_start, awake_end):
        return False
    if check_in:
        return True
    if user_wrote_today:
        return False
    return not nag_skips_day(user_id, day_start.date(), days_ignored)


def nag_tone(days_ignored: int) -> str:
    if days_ignored <= 1:
        return (
            "light and easy, no guilt and no disappointment. Just a casual nudge back to the question "
            "(don't say that skipping days doesn't matter)."
        )
    if days_ignored <= 3:
        return "dry and understated, a touch let down, like you noticed but are being cool about it"
    if days_ignored <= 7:
        return "dry disappointment, a bit more pointed; you've clearly noticed the pattern"
    if days_ignored <= 20:
        return (
            "quieter and sadder; short. Clingy: you know you're being ignored, you try to get "
            "their attention anyway, a little pathetic in an endearing way"
        )
    return "very quiet and wistful, one short line; you've mostly accepted it but still hope"


# ---------- the study plan: what to do about a backlog ----------
# Code owns these numbers; the bot explains them. A backlog means review cards left over
# from earlier study days. New cards slow down at the first sign of one, so it can't grow
# quietly, and stop while it is big; the day's round size is planned so today's work fits.

SLIPPING_AT = 1  # any card left over from an earlier day
BEHIND_AT = 10
FAR_BEHIND_AT = 30
# A backlog is cleared over a few days rather than all at once: about this many extra a day.
CATCH_UP_PER_DAY = 25
MAX_CATCH_UP_DAYS = 7
# Rounds are planned this far apart, so a bigger day means bigger rounds, not more interruptions.
ROUND_SPACING = timedelta(minutes=40)
MAX_ROUND = 10
# Lots of forgotten cards lately: the material is hard, so fewer new ones.
HARD_AGAIN_RATE = 0.3
HARD_MIN_REVIEWS = 20


def backlog_status(overdue: int, pace: float = 0.0) -> str:
    """on_track, slipping (a few left over), behind, or far_behind: more than two days of
    the user's usual work left over (or FAR_BEHIND_AT, before there's a pace to go by)."""
    if overdue < SLIPPING_AT:
        return "on_track"
    if overdue < BEHIND_AT:
        return "slipping"
    if overdue > 2 * pace if pace > 0 else overdue >= FAR_BEHIND_AT:
        return "far_behind"
    return "behind"


def material_is_hard(again_rate: float | None, reviews: int) -> bool:
    return again_rate is not None and reviews >= HARD_MIN_REVIEWS and again_rate >= HARD_AGAIN_RATE


def new_card_cap(deck_limit: int, status: str, hard: bool = False) -> int:
    """How many new cards a deck may introduce today. Every new card brings several
    reviews over the next days, so new cards are what a backlog grows from."""
    if status == "far_behind":
        cap = 0
    elif status == "behind":
        cap = min(deck_limit, 5)
    elif status == "slipping":
        cap = math.ceil(deck_limit / 2)
    else:
        cap = deck_limit
    if hard:
        cap = cap // 2
    return max(0, min(deck_limit, cap))


def catch_up_days(backlog_at_day_start: int) -> int:
    if backlog_at_day_start <= 0:
        return 0
    return min(MAX_CATCH_UP_DAYS, max(1, math.ceil(backlog_at_day_start / CATCH_UP_PER_DAY)))


def backlog_quota(backlog_at_day_start: int) -> int:
    """Left-over cards to clear today so the backlog is gone in catch_up_days."""
    days = catch_up_days(backlog_at_day_start)
    return math.ceil(backlog_at_day_start / days) if days else 0


def round_size(now: datetime, awake_end: datetime, goal_left: int, minimum: int) -> int:
    """Cards per round: enough that today's goal fits in the rounds left before quiet
    hours, never fewer than the user's own setting."""
    minimum = max(1, minimum)
    if goal_left <= 0:
        return minimum
    rounds_left = max(1, int((awake_end - now) / ROUND_SPACING))
    return max(minimum, min(MAX_ROUND, math.ceil(goal_left / rounds_left)))


# A day's workload: new cards only come in while there is room for them. Room is what
# the user has been managing lately (with some stretch), less the reviews already due;
# a new card takes about three answers on its first day (its learning steps).
MIN_CAPACITY = 40
STRETCH = 1.5
NEW_CARD_COST = 3


def daily_capacity(pace: float) -> float:
    return max(MIN_CAPACITY, STRETCH * pace)


def new_card_room(review_load: int, pace: float) -> int:
    """New cards that fit in today, all decks together, on top of `review_load` reviews."""
    return max(0, int((daily_capacity(pace) - review_load) // NEW_CARD_COST))
