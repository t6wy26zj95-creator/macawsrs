"""Today's study plan: where the user stands and what the bot does about it.

Everything here is computed by code from the cards and the review log. The plan
message the user sees is written here too, so its numbers are always right; the
bot talks about the plan but never makes up its numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    cards_per_session: int  # the user's own setting
    new_left: int  # new cards still coming in today
    new_cap: int  # new cards allowed today, all decks together
    new_limit: int  # the decks' own daily new limits, together
    new_reasons: list[str] = field(default_factory=list)  # backlog, hard, workload
    pace: float = 0.0  # answers a day lately
    again_rate: float | None = None
    capacity: int = 0  # answers a day the plan counts on

    @property
    def bigger_rounds(self) -> bool:
        return self.round_size > self.cards_per_session

    @property
    def fewer_new(self) -> bool:
        return self.new_cap < self.new_limit

    @property
    def normal(self) -> bool:
        return self.status == "on_track" and not self.bigger_rounds and not self.fewer_new

    @property
    def key(self) -> str:
        """Changes when the plan changes enough to tell the user about it."""
        return f"{self.status}|{int(self.bigger_rounds)}|{int(self.fewer_new)}"


def build(store: Store, user: Mapping[str, Any], now: datetime) -> Plan:
    b = srs.backlog(store, user, now)
    status = srs.status_of(b)
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
    new = srs.new_card_limits(store, user, now)

    cps = max(1, user["cards_per_session"])
    if srs.is_quiet(user, now):
        # Studying at night is the user's choice; the plan doesn't push bigger rounds then.
        size = cps
    else:
        size = pacing.round_size(now, srs.awake_end(user, now), goal_left, cps)
    return Plan(
        status=status,
        overdue=b["overdue"],
        overdue_at_start=at_start,
        quota=quota,
        catch_up_days=pacing.catch_up_days(at_start),
        goal_left=goal_left,
        round_size=size,
        cards_per_session=cps,
        new_left=new_left,
        new_cap=new["allowed"],
        new_limit=new["limit"],
        new_reasons=new["reasons"],
        pace=b["pace"],
        again_rate=b["again_rate"],
        capacity=new["capacity"],
    )


STATUS_WORDS = {
    "on_track": "on track (nothing left over from earlier days)",
    "slipping": "slipping (a few cards left over from earlier days)",
    "behind": "behind",
    "far_behind": "far behind",
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _new_cards_why(p: Plan) -> str:
    why = {
        "backlog": "until the left-over cards are cleared",
        "workload": "since today's reviews already make a full day and every new card brings more reviews",
        "hard": "since a lot of cards were forgotten lately",
    }
    return ", and ".join(why[r] for r in p.new_reasons) or "to keep the workload steady"


def message(p: Plan) -> str:
    """The plan in plain words, written by code so the numbers are exact. No clock times."""
    if p.normal:
        if p.goal_left:
            return f"Today's plan: {_plural(p.goal_left, 'card')} to go, nothing left over from earlier days. Normal pace."
        return "Today's plan: all done for today, nothing left over."
    parts = [f"Today's plan: {_plural(p.goal_left, 'card')} to go today."]
    if p.overdue:
        days = _plural(p.catch_up_days, "day")
        parts.append(
            f"You have {_plural(p.overdue, 'card')} left over from earlier days. Left alone that pile only "
            "grows and the words get forgotten, so "
            + ("they're all in today's number." if p.catch_up_days == 1 else
               f"I'm clearing about {p.quota} a day (that's in today's number): about {days} to catch up.")
        )
    if p.fewer_new:
        if p.new_cap == 0:
            parts.append(f"No new cards today, {_new_cards_why(p)}.")
        else:
            parts.append(f"Only {p.new_cap} new cards today instead of {p.new_limit}, {_new_cards_why(p)}.")
    if p.bigger_rounds:
        parts.append(f"I'll ask {p.round_size} in a row each time, so it all fits in before the day ends.")
    return " ".join(parts)


def describe(p: Plan) -> str:
    """The STUDY PLAN line of the context."""
    s = (
        f"STUDY PLAN (computed by code; you own it and explain it, never change the numbers): "
        f"{STATUS_WORDS[p.status]}. In words the user has seen: \"{message(p)}\""
    )
    s += f" Workload the plan counts on: about {p.capacity} answers a day."
    if p.pace:
        s += f" Lately the user has done about {round(p.pace)} answers a day."
    if p.again_rate is not None:
        s += f" Forgotten (Again) lately: {round(p.again_rate * 100)}% of reviews."
    return s


RANK = {"on_track": 0, "slipping": 1, "behind": 2, "far_behind":3}


def news(p: Plan, said: str | None) -> str:
    """The plan message to show at the start of a round, or "" when nothing changed.
    `said` is the plan key last shown this study day (None: nothing yet)."""
    if p.key == said:
        return ""
    if p.normal:
        if said is None:
            return ""
        return "Back on track: nothing left over from earlier days, new cards back to normal."
    if said is not None and RANK[p.status] < RANK.get(said.split("|")[0], 0) and p.overdue:
        return f"Progress: down to {_plural(p.overdue, 'left-over card')}. " + message(p)
    return message(p)
