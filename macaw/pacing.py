"""When to bring up the next card, and when to send a reminder.

Pure functions over plain values so they are easy to test.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

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
