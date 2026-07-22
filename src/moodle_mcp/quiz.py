"""Extract exam-prep flashcards from Moodle quiz attempt reviews.

``mod_quiz_get_attempt_review`` returns, per question, the fully rendered
review HTML — the same markup Moodle shows the student in the browser:
question text, the student's own answer, the correct answer, and a CSS
class on the question wrapper encoding correctness (``correct`` /
``partiallycorrect`` / ``incorrect`` / ``notanswered``).

Rather than re-implementing per-question-type parsing (multichoice,
matching, cloze, essay, … all render differently), we parse that HTML with
BeautifulSoup and pull out the handful of well-known CSS classes Moodle's
default renderer has used across versions. Question types without a
``.rightanswer`` block (e.g. essay) fall back to a clear placeholder rather
than a wrong guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from bs4 import BeautifulSoup

from .html_utils import html_to_plaintext
from .paths import sanitize_path_component


_STATE_CLASSES = ("correct", "partiallycorrect", "incorrect", "notanswered")
_NO_ANSWER_PLACEHOLDER = (
    "(keine automatisch erkennbare Musterlösung für diesen Fragetyp — "
    "bitte im Original in Moodle prüfen)"
)


@dataclass(frozen=True)
class Flashcard:
    slot: int
    question: str
    correct_answer: str
    your_answer: Optional[str]
    state: Optional[str]  # "correct" | "partiallycorrect" | "incorrect" | "notanswered" | None


def _text_of(node: Any) -> str:
    if node is None:
        return ""
    return html_to_plaintext(str(node))


def extract_flashcards_from_review(questions: list[dict[str, Any]]) -> list[Flashcard]:
    """Turn ``mod_quiz_get_attempt_review``'s ``questions`` list into flashcards.

    Skips questions with no recognizable ``.qtext`` (e.g. description-only
    "questions" that carry no actual content).
    """
    cards: list[Flashcard] = []
    for q in questions:
        html = q.get("html") or ""
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")

        qdiv = soup.select_one("div.que")
        classes = set(qdiv.get("class") or []) if qdiv is not None else set()
        state = next((c for c in _STATE_CLASSES if c in classes), None)

        qtext_el = soup.select_one(".qtext")
        question_text = _text_of(qtext_el)
        if not question_text:
            continue

        rightanswer_el = soup.select_one(".rightanswer")
        correct_answer = _text_of(rightanswer_el) or _NO_ANSWER_PLACEHOLDER

        ablock_el = soup.select_one(".ablock")
        your_answer = _text_of(ablock_el) or None

        slot = q.get("slot")
        try:
            slot_int = int(slot) if slot is not None else len(cards) + 1
        except (TypeError, ValueError):
            slot_int = len(cards) + 1

        cards.append(
            Flashcard(
                slot=slot_int,
                question=question_text,
                correct_answer=correct_answer,
                your_answer=your_answer,
                state=state,
            )
        )
    return cards


def render_flashcards_markdown(quiz_name: str, attempt_no: Any, flashcards: list[Flashcard]) -> str:
    """Render flashcards in the Obsidian "Spaced Repetition" plugin format.

    Cards use the multi-line ``Question\\n?\\nAnswer`` syntax (blank line
    between cards) — this is a widely supported, plugin-agnostic format
    that also reads fine as plain markdown if no flashcard plugin is
    installed.
    """
    lines: list[str] = [
        "---",
        "type: moodle-quiz-flashcards",
        f"quiz: {sanitize_path_component(quiz_name)}",
        "tags: [moodle, quiz, flashcards]",
        "---",
        "",
        f"# Flashcards — {quiz_name} (Versuch {attempt_no})",
        "",
    ]
    if not flashcards:
        lines.append("_Keine auswertbaren Fragen in diesem Versuch gefunden._")
        return "\n".join(lines).rstrip() + "\n"

    lines.append(
        "> Format kompatibel mit dem Obsidian-Plugin *Spaced Repetition* "
        "(Frage / `?` / Antwort, je Karte durch Leerzeile getrennt)."
    )
    lines.append("")

    for card in flashcards:
        marker = {
            "correct": "✅",
            "partiallycorrect": "🟡",
            "incorrect": "❌",
            "notanswered": "➖",
        }.get(card.state or "", "")
        lines.append(f"<!-- Frage {card.slot}{' ' + marker if marker else ''} -->")
        lines.append(card.question)
        lines.append("?")
        lines.append(card.correct_answer)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
