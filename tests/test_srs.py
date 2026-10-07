from __future__ import annotations

from datetime import timedelta

from macaw import srs
from macaw.db import parse

from .conftest import UID, at


def make_deck(store, n=3, new_per_day=20):
    deck_id = store.create_deck(UID, "English", "vocabulary", ["word", "meaning", "example", "notes"])
    store.update_deck(deck_id, new_per_day=new_per_day)
    for i in range(n):
        store.add_note(deck_id, {"word": f"w{i}", "meaning": f"m{i}"}, f"w{i}", reverse=False)
    return deck_id


def test_new_card_good_goes_to_learning_then_review(store, user):
    make_deck(store, 1)
    card = store.user_cards(UID)[0]
    now = at("2026-10-07 10:00")
    log_id = srs.grade(store, user, card["id"], 3, "claude", "fine", now)
    row = store.card(card["id"])
    assert row["state"] == 1  # learning
    assert parse(row["due"]) == now + timedelta(minutes=10)
    assert store.review(log_id)["rating"] == 3


def test_regrade_replaces_the_review(store, user):
    make_deck(store, 1)
    card = store.user_cards(UID)[0]
    now = at("2026-10-07 10:00")
    log_id = srs.grade(store, user, card["id"], 1, "claude", "wrong", now)
    assert store.card(card["id"])["state"] == 1
    srs.regrade(store, user, log_id, 4)
    row = store.card(card["id"])
    assert row["state"] == 2  # Easy on a new card graduates straight to review
    assert parse(row["due"]) > now + timedelta(days=1)
    assert store.review(log_id)["source"] == "user"


def test_regrade_refuses_older_review(store, user):
    make_deck(store, 1)
    card = store.user_cards(UID)[0]
    first = srs.grade(store, user, card["id"], 1, "claude", None, at("2026-10-07 10:00"))
    srs.grade(store, user, card["id"], 3, "claude", None, at("2026-10-07 10:05"))
    try:
        srs.regrade(store, user, first, 3)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_due_queue_respects_new_limit_and_learning_first(store, user):
    make_deck(store, 5, new_per_day=2)
    now = at("2026-10-07 10:00")
    queue = srs.due_queue(store, user, now)
    assert len(queue) == 2
    srs.grade(store, user, queue[0]["id"], 1, "claude", None, now)
    later = now + timedelta(minutes=2)
    queue = srs.due_queue(store, user, later)
    # The failed card is back first (1 min step); one new card left in today's limit.
    assert queue[0]["state"] == 1
    assert [c["state"] for c in queue] == [1, 0]


def test_wrong_card_comes_back_same_day(store, user):
    make_deck(store, 1)
    card = store.user_cards(UID)[0]
    now = at("2026-10-07 10:00")
    srs.grade(store, user, card["id"], 3, "claude", None, now)
    srs.grade(store, user, card["id"], 3, "claude", None, now + timedelta(minutes=10))
    review_due = parse(store.card(card["id"])["due"])
    srs.grade(store, user, card["id"], 1, "claude", None, review_due)
    row = store.card(card["id"])
    assert row["state"] == 3  # relearning
    assert parse(row["due"]) - review_due == timedelta(minutes=10)


def test_buried_card_is_hidden(store, user):
    make_deck(store, 1)
    card = store.user_cards(UID)[0]
    now = at("2026-10-07 10:00")
    store.bury(card["id"], srs.next_day_start(user, now))
    assert srs.due_queue(store, user, now) == []
    assert len(srs.due_queue(store, user, now + timedelta(days=1))) == 1


def test_quiet_hours_wrap_midnight(user):
    user = {**user, "timezone": "UTC", "quiet_start": "00:00", "quiet_end": "08:00"}
    assert srs.is_quiet(user, at("2026-10-07 03:00"))
    assert not srs.is_quiet(user, at("2026-10-07 12:00"))
    user["quiet_start"] = "23:00"
    assert srs.is_quiet(user, at("2026-10-07 23:30"))
    assert srs.is_quiet(user, at("2026-10-07 07:59"))
    assert not srs.is_quiet(user, at("2026-10-07 08:00"))


def test_day_rolls_over_at_end_of_quiet_hours(user):
    user = {**user, "timezone": "UTC", "quiet_start": "00:00", "quiet_end": "08:00"}
    # 01:00 still belongs to the previous study day.
    assert srs.day_start(user, at("2026-10-08 01:00")) == at("2026-10-07 08:00")
    assert srs.day_start(user, at("2026-10-08 09:00")) == at("2026-10-08 08:00")
    assert srs.awake_end(user, at("2026-10-08 09:00")) == at("2026-10-09 00:00")


def test_card_in_learning_counts_as_due_today(store, user):
    make_deck(store, 1)
    card = store.user_cards(UID)[0]
    now = at("2026-10-07 10:00")
    srs.grade(store, user, card["id"], 3, "claude", None, now)
    assert srs.due_queue(store, user, now) == []  # not askable yet
    later = srs.coming_back_today(store, user, now)
    assert [c["id"] for c in later] == [card["id"]]
    assert len(srs.due_today(store, user, now)) == 1
