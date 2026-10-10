"""The study plan: where the user stands, today's plan, and the progress report the bot reads.

The bot is the user's teacher: each study day it reads the report, sets the day's plan
itself (set_today_plan), starting from the numbers suggested here, and explains it in
its own words. The code keeps the numbers exact and within limits, and acts on the
plan: how many new cards come in and how many cards a round.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from . import pacing, srs
from .db import Store, parse

# Telling the user they're behind today's target: not before this share of the plan's
# day has gone, and at most this often.
NUDGE_AFTER = 0.4
NUDGE_EVERY = timedelta(hours=3)


@dataclass
class Plan:
    status: str  # on_track, slipping, behind, far_behind
    overdue: int  # studied cards left over from earlier days, still undone
    overdue_at_start: int  # how many there were when today began
    quota: int  # left-over cards to clear today
    overdue_today: int  # of those, still to do today
    catch_up_days: int
    goal_left: int  # cards still to do today
    round_size: int
    new_left: int  # new cards still coming in today
    new_cap: int  # new cards allowed today, all decks together
    new_limit: int  # the decks' own daily new limits (as far as they have new cards left)
    suggested_new: int  # what the code suggests for today
    suggested_round: int
    new_reasons: list[str] = field(default_factory=list)  # why the suggestion is lower: backlog, hard, workload
    pace: float = 0.0  # answers a day lately
    again_rate: float | None = None
    expected_again: float | None = None  # what the scheduler expected, given how long cards waited
    studied_reviews: int = 0
    long_overdue: int = 0
    # Today's plan as set (by the bot, or by the code when the bot didn't), if any.
    set_by: str | None = None
    reason: str | None = None
    target: int = 0  # cards to do today when the plan was made
    made_at: datetime | None = None
    # Today's due cards by where they come from.
    due_left_over: int = 0
    due_scheduled: int = 0
    learning: int = 0
    behind_by: int = 0  # cards behind an even pace towards today's target (0 = on pace)
    planned_round: int = 1  # the round size the plan set
    fixed_round: bool = False  # the user asked for this round size: it never grows
    max_cards: int | None = None  # the user asked for a lighter day: at most this many cards today

    @property
    def has_plan(self) -> bool:
        return self.set_by is not None


def _due_breakdown(store: Store, user: Mapping[str, Any], now: datetime) -> dict[str, int]:
    ds = srs.day_start(user, now)
    out = {"left_over": 0, "scheduled": 0, "learning": len(srs.coming_back_today(store, user, now)), "new": 0}
    for c in srs.due_queue(store, user, now):
        if c["state"] == srs.NEW:
            out["new"] += 1
        elif srs._left_over(c, ds, now):
            out["left_over"] += 1
        elif c["state"] in (1, 3):
            out["learning"] += 1
        else:
            out["scheduled"] += 1
    return out


def build(store: Store, user: Mapping[str, Any], now: datetime) -> Plan:
    b = srs.backlog(store, user, now)
    status = srs.status_of(b)
    ds = srs.day_start(user, now)
    at_start = b["overdue"] + b["overdue_done"]
    quota = pacing.backlog_quota(at_start)
    overdue_left = max(0, min(b["overdue"], quota - b["overdue_done"]))

    due = _due_breakdown(store, user, now)
    # Any due card done counts toward today's target: left-over cards done beyond today's
    # share stand in for scheduled ones (those carry over to tomorrow instead).
    extra = max(0, b["overdue_done"] - quota)
    goal_left = max(0, due["scheduled"] + due["learning"] + overdue_left + due["new"] - extra)
    limits = srs.new_card_limits(store, user, now)
    cps = max(1, user["cards_per_session"])
    awake_end = srs.awake_end(user, now)

    row = store.day_plan(user["id"], ds.date().isoformat())
    # The user's own round size: chosen today, or kept from an earlier day (their setting).
    user_fixed = bool(user["round_fixed"]) if "round_fixed" in user.keys() else False  # noqa: SIM118 (sqlite rows too)
    fixed = bool(row and row["fixed_round"]) or user_fixed
    if row and row["fixed_round"]:
        minimum = row["round_size"]
    elif user_fixed:
        minimum = cps
    else:
        minimum = max(cps, row["round_size"]) if row else cps
    capped = row["max_cards"] if row and row["max_cards"] is not None else None
    if capped is not None:
        # A lighter day the user asked for: no more than that many cards in all today.
        done = len(srs.done_today(store, user, now)["card_ids"])
        goal_left = min(goal_left, max(0, capped - done))
    # The round size is set once with the day's plan and stays all day: a size that moves
    # as cards get done or the evening comes reads like the teacher changing its mind.
    size = minimum

    p = Plan(
        status=status,
        overdue=b["overdue"],
        overdue_at_start=at_start,
        quota=quota,
        overdue_today=overdue_left,
        catch_up_days=pacing.catch_up_days(at_start),
        goal_left=goal_left,
        round_size=size,
        planned_round=minimum,
        fixed_round=fixed,
        max_cards=capped,
        new_left=due["new"],
        new_cap=row["new_cards"] if row else limits["allowed"],
        new_limit=limits["limit"],
        suggested_new=limits["allowed"],
        suggested_round=cps if user_fixed else pacing.round_size(now, awake_end, goal_left, cps),
        new_reasons=limits["reasons"],
        pace=b["pace"],
        again_rate=b["again_rate"],
        expected_again=b["expected_again"],
        studied_reviews=b["studied_reviews"],
        long_overdue=b["long_overdue"],
        due_left_over=due["left_over"],
        due_scheduled=due["scheduled"],
        learning=due["learning"],
    )
    if row:
        p.set_by, p.reason, p.target = row["set_by"], row["reason"], row["target"]
        p.made_at = parse(row["created_at"])
        # Compare with an even pace from when the plan was made to the end of the awake day.
        span = (awake_end - p.made_at).total_seconds()
        if span > 0 and now < awake_end:
            elapsed = min(1.0, max(0.0, (now - p.made_at).total_seconds() / span))
            if elapsed >= NUDGE_AFTER:
                p.behind_by = max(0, round(goal_left - p.target * (1 - elapsed)))
    return p


def set_today(store: Store, user: Mapping[str, Any], now: datetime, new_cards: int, round_size: int,
              reason: str | None, set_by: str, fixed_round: bool = False,
              max_cards: int | None = None) -> Plan:
    """Save today's plan, within limits, with today's target worked out from it."""
    day = srs.day_start(user, now).date().isoformat()
    limits = srs.new_card_limits(store, user, now)
    new_cards = max(0, min(int(new_cards), limits["limit"]))
    round_size = max(1, min(int(round_size), pacing.MAX_ROUND))
    if fixed_round:
        # The user's own choice of round size holds on later days too, as their setting.
        store.update_user(user["id"], cards_per_session=round_size, round_fixed=1)
    if max_cards is not None:
        max_cards = max(0, int(max_cards))
    store.set_day_plan(user["id"], day, new_cards, round_size, 0, reason, set_by, now, fixed_round, max_cards)
    user = dict(store.get_user(user["id"]))
    target = build(store, user, now).goal_left  # what's left once the plan's new cards are counted
    store.set_day_plan(user["id"], day, new_cards, round_size, target, reason, set_by, now, fixed_round, max_cards)
    return build(store, user, now)


STATUS_WORDS = {
    "on_track": "on track, nothing left over from earlier days",
    "slipping": "slipping, a few cards left over from earlier days",
    "behind": "behind, a real pile of cards left over from earlier days",
    "far_behind": "far behind, a big pile of cards left over from earlier days",
}

WHY_FEWER_NEW = {
    "backlog": "cards are left over from earlier days",
    "workload": "today's reviews already fill a normal day",
    "hard": "many cards were forgotten lately",
}


def _day_line(d: dict[str, Any], tz: Any) -> str:
    s = f"  {d['day'].astimezone(tz):%a %d %b}: "
    if d["due"] is not None:
        s += f"{d['due']} reviews due"
        s += f" ({d['left_over']} of them left over from before); " if d["left_over"] else "; "
    if not d["answers"]:
        s += "nothing done"
    else:
        s += f"{d['answers']} answers on {d['cards']} cards, {d['new']} new started"
        if d["studied"]:
            s += f", {d['forgotten']} of {d['studied']} reviews forgotten"
    if d["left_after"] is not None:
        s += f"; {d['left_after']} left over at the end"
    return s


def report(store: Store, user: Mapping[str, Any], now: datetime, p: Plan) -> list[str]:
    """The progress report and today's plan, for the context."""
    tz = srs.tz_of(user)
    lines = ["PROGRESS REPORT (exact, from the review log; read it like a teacher reads a student's record):"]
    days = srs.history(store, user, now)
    # Days before any record (the bot wasn't in use yet) say nothing.
    while days and not (days[0]["answers"] or days[0]["due"] is not None):
        days.pop(0)
    if days:
        lines.append("Last 7 study days:")
        lines.extend(_day_line(d, tz) for d in days)
    else:
        lines.append("No study history from the last 7 days yet.")
    lines.append(
        f"Today's due cards: {p.due_left_over} left over from earlier days, {p.due_scheduled} "
        f"review{'s' if p.due_scheduled != 1 else ''} "
        f"scheduled for today, {p.learning} in learning steps (now or later today), {p.new_left} new still "
        f"to come today. Still to do today: {p.goal_left}"
        + (
            f" (a lighter day the user asked for: at most {p.max_cards} cards in all today)."
            if p.max_cards is not None else
            f" (today's target is all of today's scheduled cards plus about {p.quota} of the left-over "
            "ones; any due card done counts toward it)." if p.overdue_at_start else "."
        )
    )
    if srs.is_quiet(user, now) and (p.overdue or p.due_scheduled):
        start = srs.next_day_start(user, now).astimezone(tz)
        lines.append(
            f"It's quiet hours: the next study day starts at {start:%H:%M}. Then today's scheduled cards "
            f"that are still undone become left over too: about {p.overdue + p.due_scheduled} left over at the "
            "start of it, unless some are done before. The rest of this report is about the study day that's "
            "ending; when the new one starts, today's target and plan are worked out again from those numbers. "
            "Use that when talking about tomorrow or the morning, and don't promise the new day's numbers yet."
        )
    s = f"Overall: {STATUS_WORDS[p.status]}."
    if p.overdue_at_start and not p.overdue:
        s += f" The {p.overdue_at_start} left over at the start of today are all done."
    elif p.overdue_at_start:
        days_n = -(-p.overdue // max(1, p.quota))
        s += (
            f" {p.overdue_at_start} were left over at the start of today ({p.overdue} still are); doing about "
            f"{p.quota} of them a day on top of each day's scheduled reviews clears them in about {days_n} "
            f"day{'s' if days_n != 1 else ''}."
        )
    if p.pace:
        s += f" Lately about {round(p.pace)} answers a day."
    if p.again_rate is not None:
        normal = round((1 - float(user["desired_retention"])) * 100)
        s += (
            f" Forgotten lately: {round(p.again_rate * 100)}% of {p.studied_reviews} reviews (not counting "
            f"learning steps)."
        )
        if p.long_overdue:
            s += (
                f" {p.long_overdue} of those reviews were cards more than a month overdue, and from how long "
                f"each card had waited the scheduler expected about {round(p.expected_again * 100)}% to be "
                f"forgotten. Forgetting long-overdue cards is expected, not a failure; compare with that "
                f"figure, not with the {normal}% the scheduler aims for on cards reviewed on time."
            )
        else:
            s += (
                f" From how long each card had waited, the scheduler expected about "
                f"{round(p.expected_again * 100)}%; on cards reviewed on time it aims for about {normal}%."
            )
    if p.overdue_at_start:
        s += (
            " Left over is counted fresh each morning: whatever of a day's scheduled cards isn't done becomes "
            "left over the next day, so a day that clears the old pile can still leave a new one. Progress "
            "shows in the daily record above (done against due), not only in this number."
        )
    lines.append(s)

    ahead = srs.upcoming(store, user, now)
    if ahead and p.pace:
        per_day = round(sum(n for _, n in ahead) / 7)
        if per_day > p.pace * 1.1:
            lines.append(
                f"Reviews already scheduled for the next 7 days: about {per_day} a day, more than the "
                f"{round(p.pace)} a day done lately. At that pace more cards are left over each day, on top "
                f"of today's."
            )
    fresh = srs.not_reviewed_here(store, user)
    if fresh:
        lines.append(
            f"{fresh} studied cards came in with an Anki import and haven't been reviewed here yet. Their "
            f"due dates come from Anki (and from spreading the overdue ones over days at import), so part "
            f"of what's due, left over and forgotten is that old deck coming back after the gap, not "
            f"something the user did wrong here."
        )

    why = ", ".join(WHY_FEWER_NEW[r] for r in p.new_reasons)
    suggestion = (
        f"{p.suggested_new} new cards (the decks allow {p.new_limit}" + (f"; fewer because {why}" if why else "")
        + f"), {p.suggested_round} card{'s' if p.suggested_round != 1 else ''} a round"
    )
    if p.fixed_round and p.goal_left and not srs.is_quiet(user, now):
        rounds = max(1, int((srs.awake_end(user, now) - now) / pacing.ROUND_SPACING))
        fits = rounds * p.round_size
        if fits < p.goal_left:
            lines.append(
                f"At the {p.round_size} cards a round the user chose, the rounds left today fit about {fits} "
                f"cards, fewer than the {p.goal_left} still to do: the rest carries over unless they take "
                "bigger or extra rounds. Be honest about that when it comes up; don't press it every message."
            )
    if not p.new_limit and not p.new_left:
        lines.append(
            "No new cards are waiting in the decks today, so there are no new words to hold back: "
            "don't talk about stopping or slowing new words."
        )
    if p.has_plan:
        by = "you set it" if p.set_by == "bot" else "set by the code because you didn't"
        s = f"TODAY'S PLAN ({by}): {p.new_cap} new cards, "
        if p.fixed_round:
            s += f"{p.round_size} cards a round (the user chose that size, so it stays); "
        else:
            s += f"{p.round_size} cards a round (stays all day); "
        s += f"target when it was made: {p.target} cards."
        if p.max_cards is not None:
            s += f" The user asked for a lighter day: at most {p.max_cards} cards today in all."
        if p.reason:
            s += f" Your reasoning then: {p.reason}"
        if p.behind_by:
            s += f" The user is {p.behind_by} cards behind an even pace towards it."
        s += f" (What the code would suggest now: {suggestion}.)"
    else:
        s = f"TODAY'S PLAN: not set yet. The code suggests {suggestion}."
    lines.append(s)
    return lines


def nudge_due(p: Plan, now: datetime, last_nudge: datetime | None) -> bool:
    """Time to tell the user they're behind today's target."""
    if not p.has_plan or p.behind_by < max(5, p.round_size):
        return False
    return last_nudge is None or now - last_nudge >= NUDGE_EVERY


def mentions_plan(text: str, p: Plan) -> bool:
    """A check-in that really talks about the day has at least one of its numbers in it."""
    numbers = {p.goal_left, p.target, p.new_cap, p.overdue, p.overdue_at_start, p.round_size} - {0, 1}
    if not numbers:
        return True
    return bool({int(n) for n in re.findall(r"\d+", text)} & numbers)
