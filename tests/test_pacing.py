from __future__ import annotations

from datetime import timedelta

from macaw import pacing
from macaw.leak import contains_answer

from .conftest import at


def test_gap_spreads_sessions_over_the_day():
    now = at("2026-10-07 10:00")
    end = at("2026-10-08 00:00")  # 14 h left
    gap = pacing.gap_until_next_session(now, end, due_count=13, cards_per_session=1)
    assert gap == timedelta(hours=1)
    assert pacing.gap_until_next_session(now, end, 1000, 1) == pacing.MIN_GAP
    assert pacing.gap_until_next_session(now, end, 1, 1) == pacing.MAX_GAP


def test_no_asking_in_quiet_hours_unless_user_is_here():
    now = at("2026-10-07 02:00")
    kw = dict(has_due=True, active_card=False, next_ask_at=None, last_bot_at=None)
    assert not pacing.should_ask(now, quiet=True, last_user_at=None, **kw)
    assert not pacing.should_ask(now, quiet=True, last_user_at=now - timedelta(seconds=30), **kw)  # cooldown
    assert pacing.should_ask(now, quiet=True, last_user_at=now - timedelta(minutes=5), **kw)
    assert not pacing.should_ask(now, quiet=True, last_user_at=now - timedelta(hours=1), **kw)


def test_no_new_card_while_one_is_open_or_before_next_ask():
    now = at("2026-10-07 12:00")
    assert not pacing.should_ask(now, has_due=True, active_card=True, next_ask_at=None,
                                 quiet=False, last_user_at=None, last_bot_at=None)
    assert not pacing.should_ask(now, has_due=True, active_card=False, next_ask_at=now + timedelta(minutes=1),
                                 quiet=False, last_user_at=None, last_bot_at=None)
    assert not pacing.should_ask(now, has_due=False, active_card=False, next_ask_at=None,
                                 quiet=False, last_user_at=None, last_bot_at=None)


def test_reminders_double_and_stop_at_limit():
    asked = at("2026-10-07 12:00")
    kw = dict(active_card=True, asked_at=asked, last_user_at=None, quiet=False, max_reminders=4, first_gap_min=60)
    assert not pacing.should_remind(asked + timedelta(minutes=59), reminders_today=0, streak=0, **kw)
    assert pacing.should_remind(asked + timedelta(minutes=60), reminders_today=0, streak=0, **kw)
    # Second reminder 2 h after the first (3 h after the question).
    assert not pacing.should_remind(asked + timedelta(minutes=150), reminders_today=1, streak=1, **kw)
    assert pacing.should_remind(asked + timedelta(minutes=180), reminders_today=1, streak=1, **kw)
    assert not pacing.should_remind(asked + timedelta(days=1), reminders_today=4, streak=4, **kw)
    assert not pacing.should_remind(asked + timedelta(days=1), reminders_today=0, streak=0,
                                    **{**kw, "quiet": True})


def test_one_reminder_after_quiet_hours_not_a_burst():
    # Asked at 23:36, no answer overnight; quiet hours end at 08:00.
    asked = at("2026-10-07 23:36")
    kw = dict(active_card=True, asked_at=asked, last_user_at=None, quiet=False, max_reminders=4, first_gap_min=60)
    morning = at("2026-10-08 08:00")
    assert pacing.should_remind(morning, reminders_today=0, streak=0, **kw)
    # That reminder went out; the next one waits a full 2 h from it, not from the question.
    after = dict(reminders_today=1, streak=1, last_reminder_at=morning, **kw)
    assert not pacing.should_remind(morning + timedelta(seconds=30), **after)
    assert not pacing.should_remind(morning + timedelta(minutes=119), **after)
    assert pacing.should_remind(morning + timedelta(minutes=120), **after)


def test_reminder_gap_counts_from_users_last_message():
    asked = at("2026-10-07 12:00")
    talked = at("2026-10-07 12:50")
    assert not pacing.should_remind(at("2026-10-07 13:10"), active_card=True, asked_at=asked, last_user_at=talked,
                                    quiet=False, reminders_today=0, max_reminders=4, streak=0, first_gap_min=60)


def test_leak_detection():
    assert contains_answer("It means present everywhere, basically.", "present everywhere")
    assert contains_answer("Ubiquitous!", "ubiquitous")
    assert not contains_answer("I love ubiquitousness", "ubiquitous")
    assert not contains_answer("anything", "a")


def test_first_days_of_ignoring_are_never_skipped():
    for uid in range(50):
        day = at("2026-10-07 06:00").date()
        assert not pacing.nag_skips_day(uid, day, 1)
        assert not pacing.nag_skips_day(uid, day, 2)


def test_nag_skips_some_days_but_never_three_in_a_row():
    start = at("2026-10-07 06:00").date()
    skips = [pacing.nag_skips_day(7, start + timedelta(days=n), n) for n in range(1, 200)]
    assert 20 < sum(skips) < 120
    assert not any(skips[i] and skips[i + 1] and skips[i + 2] for i in range(len(skips) - 2))
    # The same day always makes the same choice.
    assert skips == [pacing.nag_skips_day(7, start + timedelta(days=n), n) for n in range(1, 200)]


def test_nag_time_varies_and_stays_inside_the_awake_day():
    times = set()
    for n in range(30):
        ds = at("2026-10-07 06:00") + timedelta(days=n)
        t = pacing.nag_time(7, ds, ds + timedelta(hours=16))
        assert ds + timedelta(hours=1) <= t <= ds + timedelta(hours=13)
        times.add((t - ds).seconds // 3600)
    assert len(times) > 5


def test_one_nag_a_day_and_check_in_is_never_skipped():
    ds = at("2026-10-07 06:00")
    end = ds + timedelta(hours=16)
    kw = dict(user_id=7, day_start=ds, awake_end=end, days_ignored=1, quiet=False, user_wrote_today=False)
    slot = pacing.nag_time(7, ds, end)
    assert not pacing.should_nag(slot - timedelta(minutes=1), nagged_today=False, **kw)
    assert pacing.should_nag(slot, nagged_today=False, **kw)
    assert not pacing.should_nag(slot, nagged_today=True, **kw)
    assert not pacing.should_nag(slot, nagged_today=False, **{**kw, "quiet": True})
    # A day that gets skipped still gets the check-in.
    n = next(n for n in range(3, 400) if pacing.nag_skips_day(7, (ds + timedelta(days=n)).date(), 30))
    ds = ds + timedelta(days=n)
    kw.update(day_start=ds, awake_end=ds + timedelta(hours=16), days_ignored=30)
    slot = pacing.nag_time(7, ds, ds + timedelta(hours=16))
    assert not pacing.should_nag(slot, nagged_today=False, **kw)
    assert pacing.should_nag(slot, nagged_today=False, check_in=True, **kw)
