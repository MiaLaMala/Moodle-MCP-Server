"""Tests for :mod:`moodle_mcp.quiz` — quiz-review-HTML → flashcard extraction."""

from __future__ import annotations

from moodle_mcp.quiz import (
    Flashcard,
    _NO_ANSWER_PLACEHOLDER,
    extract_flashcards_from_review,
    render_flashcards_markdown,
)


def _question_html(
    state: str,
    qtext: str,
    rightanswer: str | None,
    ablock: str | None,
) -> str:
    parts = [f'<div class="que multichoice {state}">']
    parts.append(f'<div class="qtext">{qtext}</div>')
    if ablock is not None:
        parts.append(f'<div class="ablock">{ablock}</div>')
    if rightanswer is not None:
        parts.append(f'<div class="rightanswer">{rightanswer}</div>')
    parts.append("</div>")
    return "".join(parts)


def test_extract_flashcards_basic_correct_question() -> None:
    questions = [
        {
            "slot": 1,
            "html": _question_html(
                "correct",
                "Was ist 2+2?",
                "Richtige Antwort: 4",
                "Ihre Antwort: 4",
            ),
        }
    ]
    cards = extract_flashcards_from_review(questions)
    assert len(cards) == 1
    card = cards[0]
    assert card.slot == 1
    assert card.question == "Was ist 2+2?"
    assert card.correct_answer == "Richtige Antwort: 4"
    assert card.your_answer == "Ihre Antwort: 4"
    assert card.state == "correct"


def test_extract_flashcards_incorrect_and_partially_correct_states() -> None:
    questions = [
        {"slot": 1, "html": _question_html("incorrect", "Q1", "A1", "Falsch")},
        {"slot": 2, "html": _question_html("partiallycorrect", "Q2", "A2", "Halb")},
        {"slot": 3, "html": _question_html("notanswered", "Q3", "A3", None)},
    ]
    cards = extract_flashcards_from_review(questions)
    assert [c.state for c in cards] == ["incorrect", "partiallycorrect", "notanswered"]
    assert cards[2].your_answer is None


def test_extract_flashcards_falls_back_to_placeholder_without_rightanswer() -> None:
    """Essay-type questions have no ``.rightanswer`` block — must not guess."""
    questions = [
        {
            "slot": 1,
            "html": (
                '<div class="que essay notanswered">'
                '<div class="qtext">Beschreibe X.</div>'
                "</div>"
            ),
        }
    ]
    cards = extract_flashcards_from_review(questions)
    assert len(cards) == 1
    assert cards[0].correct_answer == _NO_ANSWER_PLACEHOLDER
    assert cards[0].your_answer is None


def test_extract_flashcards_skips_questions_without_qtext() -> None:
    questions = [
        {"slot": 1, "html": '<div class="que description"></div>'},
        {"slot": 2, "html": _question_html("correct", "Echte Frage", "Antwort", "Antwort")},
    ]
    cards = extract_flashcards_from_review(questions)
    assert len(cards) == 1
    assert cards[0].question == "Echte Frage"


def test_extract_flashcards_skips_empty_html() -> None:
    questions = [{"slot": 1, "html": ""}, {"slot": 2}]
    assert extract_flashcards_from_review(questions) == []


def test_extract_flashcards_slot_fallback_when_missing_or_invalid() -> None:
    questions = [
        {"html": _question_html("correct", "Q ohne slot", "A", "A")},
        {"slot": "not-a-number", "html": _question_html("correct", "Q2", "A", "A")},
    ]
    cards = extract_flashcards_from_review(questions)
    assert cards[0].slot == 1
    assert cards[1].slot == 2


def test_render_flashcards_markdown_empty_list() -> None:
    text = render_flashcards_markdown("Testquiz", 1, [])
    assert "moodle-quiz-flashcards" in text
    assert "Keine auswertbaren Fragen" in text


def test_render_flashcards_markdown_contains_question_answer_and_marker() -> None:
    cards = [
        Flashcard(
            slot=1,
            question="Was ist die Hauptstadt von Deutschland?",
            correct_answer="Berlin",
            your_answer="Berlin",
            state="correct",
        ),
        Flashcard(
            slot=2,
            question="Was ist 7*8?",
            correct_answer="56",
            your_answer="54",
            state="incorrect",
        ),
    ]
    text = render_flashcards_markdown("Testquiz", 3, cards)
    assert "quiz: Testquiz" in text
    assert "Versuch 3" in text
    assert "Was ist die Hauptstadt von Deutschland?" in text
    assert "Berlin" in text
    assert "✅" in text
    assert "❌" in text
    # Obsidian Spaced Repetition multi-line format: Question / ? / Answer
    lines = text.splitlines()
    q_idx = lines.index("Was ist die Hauptstadt von Deutschland?")
    assert lines[q_idx + 1] == "?"
    assert lines[q_idx + 2] == "Berlin"


def test_render_flashcards_markdown_sanitizes_quiz_name_in_frontmatter() -> None:
    text = render_flashcards_markdown("Quiz: Kapitel/1", 1, [])
    # sanitize_path_component strips characters unsafe for filenames
    assert "Quiz: Kapitel/1" not in text.splitlines()[2]
