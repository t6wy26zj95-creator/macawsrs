"""Today's study plan: where the user stands and what the bot does about it.

Everything here is computed by code from the cards and the review log; the bot
explains the plan but never makes up its numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from . import pacing, srs
from .db import Store


@dataclass
class Plan:
    status: str  # on_track, slipping, behind, far_behind
    overdue: int  # studied cards left over from earlier days, still undone
    overdue_at_start: int  # how many there were when today began
    quota: int  # left-over cards to clear today
    catch_up_days: int
    goal_left: int  # cards still to do today to keep to the plan
    round_size: int
    new_left: int  # new cards still coming in today
    new_cap: int  # new cards allowed today, all decks together
    new_limit: int  # the decks' own daily new limits, together
    pace: float  # answers a day lately
    again_rate: float | None
    hard: bool

    @property
    def key(self) -> str:
        """Changes when the plan changes enough to tell the user about it."""
        return self.status


def build(store: Store, user: Mapping[str, Any], now: datetime) -> Plan:
    b = srs.backlog(store, user, now)
    status = srs.status_of(b)
    hard = pacing.material_is_hard(b["again_rate"], b["studied_reviews"])
    ds = srs.day_start(user, now)
    at_start = b["overdue"] + b["overdue_done"]
    quota = pacing.backlog_quota(at_start)
    overdue_left = max(0, min(b["overdue"], quota - b["overdue_done"]))

    queue = srs.due_queue(store, user, now)
    later = srs.coming_back_today(store, user, now)
    new_left = sum(1 for c in queue if c["state"] == srs.NEW)
    left_over = sum(1 for c in queue if srs._left_over(c, ds, now))
    fresh = len(queue) - new_left - left_over + len(later)
    goal_left = fresh + overdue_left + new_left

    new_limit = new_cap = 0
    for d in store.decks(user["id"]):
        new_limit += d["new_per_day"]
        new_cap += pacing.new_card_cap(d["new_per_day"], status, hard)

    if srs.is_quiet(user, now):
        # Studying at night is the user's choice; the plan doesn't push bigger rounds then.
        size = max(1, user["cards_per_session"])
    else:
        size = pacing.round_size(now, srs.awake_end(user, now), goal_left, user["cards_per_session"])
    return Plan(
        status=status,
        overdue=b["overdue"],
        overdue_at_start=at_start,
        quota=quota,
        catch_up_days=pacing.catch_up_days(at_start),
        goal_left=goal_left,
        round_size=size,
        new_left=new_left,
        new_cap=new_cap,
        new_limit=new_limit,
        pace=b["pace"],
        again_rate=b["again_rate"],
        hard=hard,
    )


STATUS_WORDS = {
    "on_track": "on track (nothing left over from earlier days)",
    "slipping": "slipping (a few cards left over from earlier days)",
    "behind": "behind",
    "far_behind": "far behind",
}


def describe(p: Plan, cards_per_session: int) -> str:
    """The STUDY PLAN line of the context."""
    s = f"STUDY PLAN (computed by code; you own it and explain it, never change the numbers): {STATUS_WORDS[p.status]}."
    if p.overdue_at_start:
        s += (
            f" Backlog: {p.overdue} cards left over from earlier days"
            f" ({p.overdue_at_start} at the start of today). Plan: clear about {p.quota} of them a day,"
            f" so the backlog is gone in about {p.catch_up_days} day{'s' if p.catch_up_days != 1 else ''}."
        )
    if p.new_cap < p.new_limit:
        why = "the backlog" if p.status != "on_track" else "the material"
        if p.hard and p.status != "on_track":
            why = "the backlog and how often cards are being forgotten lately"
        elif p.hard:
            why = "how often cards are being forgotten lately"
        s += (
            f" New cards: {p.new_cap} today instead of the usual {p.new_limit}, because of {why}"
            " (every new card brings several reviews over the next days)."
        )
    s += f" Still to do today to stay on plan: {p.goal_left} cards."
    if p.round_size > cards_per_session:
        s += (
            f" Rounds are {p.round_size} cards in a row instead of {cards_per_session}, so today fits"
            " before quiet hours without more interruptions."
        )
    if p.pace:
        s += f" Lately the user has done about {round(p.pace)} answers a day."
    if p.again_rate is not None:
        s += f" Forgotten (Again) lately: {round(p.again_rate * 100)}% of reviews."
    return s


RANK = {"on_track": 0, "slipping": 1, "behind": 2, "far_behind": 3}


def announcement(p: Plan, said: str | None, cards_per_session: int) -> str:
    """What to tell the user at the start of a round, or "" when nothing new.
    `said` is the plan status last explained this study day (None: nothing yet)."""
    if p.key == said:
        return ""
    if said is not None and RANK[p.status] < RANK.get(said, 0):
        if p.status == "on_track":
            return (
                "The backlog is cleared. Before the card, say so in one sentence, plainly pleased, "
                "and that new cards are back to normal."
            )
        return (
            f"Progress: the backlog is down to {p.overdue}. Before the card, say so in one short "
            "sentence, and what that changes (see STUDY PLAN), without a speech."
        )
    if p.status == "on_track":
        return ""
    if p.status == "slipping":
        return (
            "A few cards were left over from earlier days. Before the card, mention in one short "
            "sentence that you're slowing new cards a little so it doesn't build up."
        )
    bigger = p.round_size > cards_per_session
    return (
        "Before the card, tell the user today's plan in two or three short sentences, like a teacher "
        "who has looked at their progress: how many cards are left over and why that matters (left "
        "alone it only grows, and overdue cards get forgotten), what you're changing today ("
        + ("bigger rounds, " if bigger else "")
        + "fewer or no new cards until it's cleared) and how long catching up takes. Use the numbers "
        "from STUDY PLAN only. Confident, not scolding. Then ask the card."
    )
