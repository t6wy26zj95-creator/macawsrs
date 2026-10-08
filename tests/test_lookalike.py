from macaw.lookalike import find_lookalikes, tokens

F = ["word", "meaning", "example", "notes"]


def _n(i, word, meaning):
    return {"id": i, "fields": F, "values": {"word": word, "meaning": meaning}, "deck": "English"}


NOTES = [
    _n(1, "mottled", "marked with spots or smears of colour"),
    _n(2, "recumbent", "lying down; reclining"),
    _n(3, "ubiquitous", "present everywhere"),
    _n(4, "kaleidoscopic", "having many different colours"),
]


def test_tokens_fold_spelling_and_endings():
    assert tokens("spotted colours") == tokens("spot colors")


def test_meaning_from_another_card_is_found_best_match_first():
    hits = find_lookalikes("It means to have spots of various kinds of colors", NOTES, exclude_note=2)
    assert [h["word"] for h in hits][0] == "mottled"
    assert all(h["note_id"] != 2 for h in hits)


def test_short_reply_naming_another_word_is_found():
    assert [h["word"] for h in find_lookalikes("mottled?", NOTES, exclude_note=2)] == ["mottled"]


def test_unrelated_reply_finds_nothing():
    assert find_lookalikes("no idea, sorry", NOTES, exclude_note=2) == []
    assert find_lookalikes("I think it's something about the weather today", NOTES, exclude_note=2) == []
