"""Unit tests for the golden-set v2 dialogue schema (T14.0.2/T14.0.3, no DB/LLM).

Covers: the schema itself (`Turn`/`Dialogue` validation), that a v1 flat record is loaded as a
one-turn `Dialogue` ("single-turn items = one-turn dialogues", T14.0.2) without changing the v1
loader at all, and that the real `eval/golden_set_v2.jsonl` fixture (T14.0.3) parses and covers
every category the task named.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.eval.dataset import (
    ALLOWED_INTENTS,
    ALLOWED_LANGUAGES,
    DEFAULT_GOLDEN_SET,
    DEFAULT_GOLDEN_SET_V2,
    Dialogue,
    GoldenItem,
    Turn,
    load_dialogues,
    load_golden_set,
)

# --- Schema validation -------------------------------------------------------------------


def test_turn_rejects_unknown_intent() -> None:
    with pytest.raises(ValueError, match="expected_intent"):
        Turn(message="hi", expected_intent="bogus", language="en")


def test_turn_rejects_unknown_language() -> None:
    with pytest.raises(ValueError, match="language"):
        Turn(message="hi", expected_intent="chitchat", language="fr")


def test_turn_requires_ground_truth_when_answerable() -> None:
    with pytest.raises(ValueError, match="ground_truth"):
        Turn(
            message="How do I prevent SQL injection?",
            expected_intent="security",
            language="en",
            answerable=True,
            ground_truth=None,
        )


def test_turn_allows_no_ground_truth_when_not_answerable() -> None:
    turn = Turn(
        message="What's the weather?",
        expected_intent="off_topic",
        language="en",
        answerable=False,
    )
    assert turn.ground_truth is None


def test_dialogue_requires_at_least_one_turn() -> None:
    with pytest.raises(ValueError, match="no turns"):
        Dialogue(id="empty", turns=())


def test_single_turn_dialogue_reports_is_single_turn() -> None:
    one_turn = (Turn(message="hi", expected_intent="chitchat", language="en"),)
    dialogue = Dialogue(id="d", turns=one_turn)
    assert dialogue.is_single_turn is True

    multi = Dialogue(
        id="m",
        turns=(
            Turn(message="hi", expected_intent="chitchat", language="en"),
            Turn(message="and then?", expected_intent="followup", language="en"),
        ),
    )
    assert multi.is_single_turn is False


# --- Backwards compatibility (T14.0.2) --------------------------------------------------


def test_legacy_row_is_wrapped_into_a_one_turn_dialogue(tmp_path: Path) -> None:
    legacy_file = tmp_path / "legacy.jsonl"
    legacy_file.write_text(
        '{"id": "x", "question": "How do I prevent SQL injection?", '
        '"ground_truth": "Use parameterized queries.", '
        '"expected_doc": "cs-sql-injection-prevention", "version": "current", '
        '"answerable": true}\n',
        encoding="utf-8",
    )
    dialogues = load_dialogues(legacy_file)
    assert len(dialogues) == 1
    assert dialogues[0].is_single_turn
    turn = dialogues[0].turns[0]
    assert turn.message == "How do I prevent SQL injection?"
    assert turn.expected_intent == "security"
    assert turn.language == "en"
    assert turn.answerable is True
    assert turn.ground_truth == "Use parameterized queries."


def test_v2_row_with_turns_is_not_wrapped(tmp_path: Path) -> None:
    v2_file = tmp_path / "v2.jsonl"
    v2_file.write_text(
        '{"id": "d1", "turns": ['
        '{"message": "hi", "expected_intent": "chitchat", "language": "en"}, '
        '{"message": "how do I prevent SQLi?", "expected_intent": "security", "language": "en", '
        '"expected_doc": "cs-sql-injection-prevention", "answerable": true, '
        '"ground_truth": "Use parameterized queries."}'
        "]}\n",
        encoding="utf-8",
    )
    dialogues = load_dialogues(v2_file)
    assert len(dialogues) == 1
    assert not dialogues[0].is_single_turn
    assert len(dialogues[0].turns) == 2


def test_mixed_legacy_and_v2_rows_in_one_file(tmp_path: Path) -> None:
    mixed_file = tmp_path / "mixed.jsonl"
    mixed_file.write_text(
        '{"id": "legacy-1", "question": "q", "ground_truth": "NOT_IN_CORPUS", '
        '"expected_doc": null, "version": null, "answerable": false}\n'
        '{"id": "v2-1", "turns": [{"message": "hola", "expected_intent": "chitchat", '
        '"language": "es"}]}\n',
        encoding="utf-8",
    )
    dialogues = load_dialogues(mixed_file)
    assert [d.id for d in dialogues] == ["legacy-1", "v2-1"]
    assert all(d.is_single_turn for d in dialogues)


def test_v1_golden_set_still_loads_unchanged_through_the_old_loader() -> None:
    """`GoldenItem`/`load_golden_set` are untouched by T14.0.2 — same 14 items as before."""
    items = load_golden_set(DEFAULT_GOLDEN_SET)
    assert len(items) == 14
    assert all(isinstance(item, GoldenItem) for item in items)


def test_v1_golden_set_also_loads_through_the_v2_loader_backwards_compatibly() -> None:
    """Every v1 item is loadable through `load_dialogues` as a one-turn `Dialogue`."""
    dialogues = load_dialogues(DEFAULT_GOLDEN_SET)
    assert len(dialogues) == 14
    assert all(d.is_single_turn for d in dialogues)
    assert all(d.turns[0].expected_intent == "security" for d in dialogues)


# --- The real golden-set v2 fixture (T14.0.3) --------------------------------------------


def test_golden_set_v2_fixture_parses_and_has_unique_ids() -> None:
    dialogues = load_dialogues(DEFAULT_GOLDEN_SET_V2)
    assert len(dialogues) >= 40
    ids = [d.id for d in dialogues]
    assert len(ids) == len(set(ids))


def test_golden_set_v2_covers_every_allowed_intent_and_language() -> None:
    dialogues = load_dialogues(DEFAULT_GOLDEN_SET_V2)
    seen_intents = {t.expected_intent for d in dialogues for t in d.turns}
    seen_languages = {t.language for d in dialogues for t in d.turns}
    assert seen_intents == set(ALLOWED_INTENTS)
    assert seen_languages == set(ALLOWED_LANGUAGES)


def test_golden_set_v2_has_multi_turn_dialogues_with_followups() -> None:
    dialogues = load_dialogues(DEFAULT_GOLDEN_SET_V2)
    multi_turn = [d for d in dialogues if not d.is_single_turn]
    assert len(multi_turn) >= 8
    # at least one follow-up resolved 1 turn back and one resolved 3+ turns back
    assert any(len(d.turns) == 2 and d.turns[1].expected_intent == "followup" for d in multi_turn)
    assert any(len(d.turns) >= 4 and d.turns[-1].expected_intent == "followup" for d in multi_turn)


def test_golden_set_v2_has_misspelling_items() -> None:
    dialogues = load_dialogues(DEFAULT_GOLDEN_SET_V2)
    messages = [t.message.lower() for d in dialogues for t in d.turns]
    assert any(m == "hol" for m in messages)
    assert any(m == "hell" for m in messages)
    assert any("hell" in m and m != "hell" for m in messages)  # "hell" inside a real sentence
    assert any("injecion" in m for m in messages)
    assert any("preparde" in m for m in messages)


def test_golden_set_v2_has_abstention_in_both_languages() -> None:
    dialogues = load_dialogues(DEFAULT_GOLDEN_SET_V2)
    abstentions = [
        t
        for d in dialogues
        for t in d.turns
        if t.expected_intent == "security" and t.answerable is False
    ]
    assert any(t.language == "en" for t in abstentions)
    assert any(t.language == "es" for t in abstentions)
