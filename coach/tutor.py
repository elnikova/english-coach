"""Тьютор: перетворює текст, фото чи файли на урок (теорія, вправи, слова)."""

import base64
import copy

from .db import DEFAULT_CATEGORIES, category_names
from .llm import LlmError, generate_json

LEVELS = ["A1", "A2", "B1", "B2", "C1"]
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_TEXT = 16_000

TUTOR_PROMPT = """\
You prepare English lessons for a Ukrainian-speaking software developer.
You receive source material: a word, a phrase, a text, photos, or documents.
Answer with a single JSON object that follows the given schema. Fill in every field.

First decide lesson_type.

"grammar" — the source explains or practises a grammar topic, or is a text or document
rich enough to teach from. Build a worksheet-style lesson:
- sections: 3-6 theory sections in this order: FORM, then rules and typical mistakes, then USE.
  Each section has a short English heading, an explanation in Ukrainian (2-4 sentences),
  and 3-6 examples. Where forms are involved, add a table (for example rows per pronoun,
  columns Positive / Negative / Question / Short answer).
  To show typical mistakes, put an incorrect example (correct=false) right before its
  corrected version (correct=true).
- exercises: 2-4 exercises with 6-10 questions each, from easier to harder. Start with
  "choose the correct option" (give 2-4 options), then gap-fill or rewrite tasks without
  options. Mark every gap as _____ in the prompt. answers lists every accepted variant
  (full and contracted forms), each exactly as the learner should type it for the gap.
- items: 5-10 key words and phrases from the material.
Cover everything the source teaches, but write your own explanations, examples and
exercises: follow the structure and the topic, do not copy the source text.

"vocabulary" — the source is a single word, a phrase, or a short list of words, so a
grammar lesson cannot be built. Leave sections and exercises empty and fill items only.

Rules for items (both lesson types):
- Keep each term in dictionary form. Prefer phrases and collocations over isolated easy words.
- forms: the main forms if the word changes, e.g. "write – writes – wrote – written – writing",
  "bug – bugs", "fast – faster – fastest". Empty string if it does not change.
- examples: if the word changes form, show it across forms, one example per form.
  Verbs: Present Simple, Present Continuous, Past Simple, Present Perfect, Future, and
  Passive or Conditional where natural (5-7 examples). Nouns: singular and plural.
  Adjectives: base, comparative, superlative. Put the tense or form name in `tense` and the
  exact word form used in the sentence in `form`.
  If the word does not change (set phrase, adverb), give 3 typical uses with empty `tense`.
- Every example has a Ukrainian translation.
- transcription: IPA, British pronunciation, in slashes.
- family: 2-5 words built from the same root but in other parts of speech, each with its
  part of speech and a Ukrainian translation. For example, for "deploy" (verb):
  deployment (noun), deployable (adjective). Do not repeat the term itself and do not list
  its grammatical forms (those belong in `forms`). Use an empty list when the word has no
  common relatives.
- synonyms: 2-4 words or phrases with a close meaning, each with its part of speech, a Ukrainian
  translation and a short Ukrainian note on how its use differs from the term. Use near-synonyms a
  learner would actually meet; leave the list empty when the word has no real synonym.

Context of examples:
- If the source is about programming or IT work, set topic_type to "programming" and take
  examples from a developer's working life: code review, stand-ups, pull requests, bug
  reports, documentation, interviews.
- Otherwise set topic_type to "general" and show the most common everyday uses; in grammar
  lessons you may add a few examples from a developer's work.

level: the CEFR level of the lesson as a whole. If the source states its level (for example
"A1" in a worksheet title or file name), use that; otherwise judge by the material.

Language: summary_uk, explanation_uk, instruction_uk, tips_uk and translations are in
Ukrainian; headings, examples, exercise prompts and definition_en are in English.
tips_uk: 1-4 short notes on typical mistakes of Ukrainian speakers.
If the source is a photo, read any text in it; if there is no text, describe what is shown
and build the vocabulary around that.
"""

EXAMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "en": {"type": "string"},
        "uk": {"type": "string"},
        "tense": {"type": "string", "description": "Tense or form name, empty if not applicable"},
        "form": {"type": "string", "description": "Exact word form used in the sentence"},
    },
    "required": ["en", "uk"],
}

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "term": {"type": "string"},
        "transcription": {"type": "string"},
        "part_of_speech": {"type": "string"},
        "forms": {"type": "string"},
        "translation_uk": {"type": "string"},
        "definition_en": {"type": "string"},
        "level": {"type": "string", "enum": LEVELS},
        "category": {"type": "string"},  # перелік підставляємо під час запиту
        "examples": {"type": "array", "items": EXAMPLE_SCHEMA},
        "synonyms": {
            "type": "array",
            "description": "Words or phrases with a close meaning",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "part_of_speech": {"type": "string"},
                    "translation_uk": {"type": "string"},
                    "note_uk": {"type": "string",
                                "description": "Short note in Ukrainian on how its use differs"},
                },
                "required": ["term", "part_of_speech", "translation_uk", "note_uk"],
            },
        },
        "family": {
            "type": "array",
            "description": "Related words from the same root in other parts of speech",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "part_of_speech": {"type": "string"},
                    "translation_uk": {"type": "string"},
                },
                "required": ["term", "part_of_speech", "translation_uk"],
            },
        },
    },
    "required": ["term", "transcription", "part_of_speech", "forms", "translation_uk",
                 "definition_en", "level", "category", "examples", "family", "synonyms"],
}

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "heading": {"type": "string"},
        "explanation_uk": {"type": "string"},
        "table": {
            "type": "object",
            "properties": {
                "headers": {"type": "array", "items": {"type": "string"}},
                "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
            },
            "required": ["headers", "rows"],
        },
        "examples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "en": {"type": "string"},
                    "uk": {"type": "string"},
                    "correct": {"type": "boolean"},
                },
                "required": ["en", "uk", "correct"],
            },
        },
    },
    "required": ["heading", "explanation_uk", "examples"],
}

EXERCISE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "instruction_uk": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "answers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["prompt", "answers"],
            },
        },
    },
    "required": ["title", "instruction_uk", "questions"],
}

LESSON_SCHEMA = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short lesson title in English"},
            "lesson_type": {"type": "string", "enum": ["grammar", "vocabulary"]},
            "level": {"type": "string", "enum": LEVELS, "description": "CEFR level of the whole lesson"},
            "topic_type": {"type": "string", "enum": ["programming", "general"]},
            "summary_uk": {"type": "string"},
            "sections": {"type": "array", "items": SECTION_SCHEMA},
            "exercises": {"type": "array", "items": EXERCISE_SCHEMA},
            "tips_uk": {"type": "array", "items": {"type": "string"}},
            "items": {"type": "array", "items": ITEM_SCHEMA},
        },
        "required": ["title", "lesson_type", "level", "topic_type", "summary_uk",
                 "sections", "exercises", "tips_uk", "items"],
}


def categories() -> list[str]:
    """Список категорій із бази; якщо база ще порожня — типові."""
    try:
        return category_names() or list(DEFAULT_CATEGORIES)
    except Exception:
        return list(DEFAULT_CATEGORIES)


def lesson_schema() -> dict:
    """Схема уроку з актуальним переліком категорій."""
    schema = copy.deepcopy(LESSON_SCHEMA)
    schema["properties"]["items"]["items"]["properties"]["category"]["enum"] = categories()
    return schema


def detect_kind(file: dict) -> str:
    """image | pdf | file (текстовий)."""
    mime = (file.get("type") or "").lower()
    name = (file.get("name") or "").lower()
    if mime in IMAGE_TYPES:
        return "image"
    if mime == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if mime.startswith(("video/", "audio/")):
        raise ValueError("Video and audio are not supported yet. Paste the text or subtitles instead.")
    return "file"


def build_lesson(text: str = "", files: list[dict] | None = None, max_items: int = 10) -> dict:
    """Надсилає джерело (текст і/або кілька файлів) моделі й повертає урок."""
    parts: list[dict] = []
    for file in files or []:
        kind = detect_kind(file)
        if kind in ("image", "pdf"):
            mime = "application/pdf" if kind == "pdf" else file["type"].lower()
            parts.append({"mime": mime, "data": file["data"]})
        else:
            try:
                decoded = base64.b64decode(file["data"]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise ValueError(
                    f"Could not read “{file.get('name', '')}” as text."
                    " Images, PDFs and text files are supported."
                ) from None
            text = f"{text}\n\n[File: {file.get('name', '')}]\n{decoded}".strip()
    request = f"Build one lesson with at most {max_items} items from this source (all parts together)."
    if text.strip():
        request += f"\n\n<source>\n{text.strip()[:MAX_TEXT]}\n</source>"
    parts.append({"text": request})

    schema = lesson_schema()
    lesson = generate_json(TUTOR_PROMPT, parts, schema)
    if not _has_content(lesson):
        lesson = generate_json(TUTOR_PROMPT, parts, schema)  # одна повторна спроба
    if not _has_content(lesson):
        raise LlmError("The model did not return a lesson. Please try again.")
    return _clean(lesson)


CARD_PROMPT = """\
You create one English vocabulary card for a Ukrainian-speaking software developer.

- term: dictionary form of the word or phrase the user asked for.
- transcription: IPA, British pronunciation, in slashes.
- forms: the main forms if the word changes ("write - writes - wrote - written - writing"),
  empty string if it does not.
- examples: if the word changes form, show it across forms, one example per form, and put the
  tense or form name in `tense` and the exact word form in `form`. Verbs: Present Simple,
  Present Continuous, Past Simple, Present Perfect, Future, plus Passive or Conditional where
  natural (5-7 examples). Nouns: singular and plural. Adjectives: base, comparative, superlative.
  A set phrase or adverb gets 3 typical uses with an empty `tense`.
  Every example has a Ukrainian translation.
- family: 2-5 words from the same root in other parts of speech, each with its part of speech and
  a Ukrainian translation. Never the term itself and never its grammatical forms. Empty if none.
- synonyms: 2-4 words or phrases with a close meaning, each with its part of speech, a Ukrainian
  translation and a short Ukrainian note on how its use differs from the term. Use near-synonyms a
  learner would actually meet; leave the list empty when the word has no real synonym.
- translation_uk in Ukrainian, definition_en in simple English.
- If the word belongs to programming or IT work, take examples from a developer's working life;
  otherwise show the most common everyday uses.
"""


def card_schema() -> dict:
    """Схема однієї картки — набагато менша за схему цілого уроку."""
    schema = copy.deepcopy(ITEM_SCHEMA)
    schema["properties"]["category"]["enum"] = categories()
    return schema


def build_card(term: str, hint: str = "") -> dict | None:
    """Картка для одного слова чи фрази. hint — значення, в якому слово
    трапилося: підказує моделі потрібний відтінок, коли їх кілька."""
    request = f"Make a card for: {term}"
    if hint:
        request += f"\nIn the text it meant: {hint[:200]}. Keep that meaning."
    item = generate_json(CARD_PROMPT, [{"text": request}], card_schema(), max_tokens=3000)
    return _clean({"items": [item]})["items"][0] if item.get("term") else None


def _has_content(lesson: dict) -> bool:
    return bool(lesson.get("items") or lesson.get("sections"))


def _dicts(value) -> list[dict]:
    return [v for v in value or [] if isinstance(v, dict)] if isinstance(value, list) else []


def _clean(lesson: dict) -> dict:
    """Підстраховка: приводимо відповідь моделі до очікуваної форми."""
    lesson.setdefault("title", "Lesson")
    lesson.setdefault("topic_type", "general")
    lesson.setdefault("summary_uk", "")
    lesson["tips_uk"] = [str(t) for t in lesson.get("tips_uk") or [] if t]

    sections = []
    for section in _dicts(lesson.get("sections")):
        table = section.get("table")
        if not (isinstance(table, dict) and table.get("rows")):
            table = None
        else:
            table = {
                "headers": [str(h) for h in table.get("headers") or []],
                "rows": [[str(c) for c in row] for row in table["rows"] if isinstance(row, list)],
            }
        sections.append({
            "heading": str(section.get("heading", "")),
            "explanation_uk": str(section.get("explanation_uk", "")),
            "table": table,
            "examples": [
                {"en": str(e["en"]), "uk": str(e.get("uk", "")), "correct": e.get("correct", True) is not False}
                for e in _dicts(section.get("examples")) if e.get("en")
            ],
        })
    lesson["sections"] = sections

    exercises = []
    for exercise in _dicts(lesson.get("exercises")):
        questions = []
        for question in _dicts(exercise.get("questions")):
            answers = question.get("answers")
            answers = [answers] if isinstance(answers, str) else list(answers or [])
            answers = [str(a) for a in answers if str(a).strip()]
            if question.get("prompt") and answers:
                questions.append({
                    "prompt": str(question["prompt"]),
                    "options": [str(o) for o in question.get("options") or []],
                    "answers": answers,
                })
        if questions:
            exercises.append({
                "title": str(exercise.get("title", "Exercise")),
                "instruction_uk": str(exercise.get("instruction_uk", "")),
                "questions": questions,
            })
    lesson["exercises"] = exercises

    items, known = [], set(categories())
    for item in _dicts(lesson.get("items")):
        if not str(item.get("term", "")).strip():
            continue
        if item.get("level") not in LEVELS:
            item["level"] = "B1"
        if item.get("category") not in known:
            item["category"] = "Other"
        item["forms"] = str(item.get("forms") or "")
        item["examples"] = [
            {"en": str(e["en"]), "uk": str(e.get("uk", "")),
             "tense": str(e.get("tense") or ""), "form": str(e.get("form") or "")}
            for e in _dicts(item.get("examples")) if e.get("en")
        ]
        for key in ("family", "synonyms"):
            seen = {str(item["term"]).strip().lower()}
            related = []
            for entry in _dicts(item.get(key)):
                term = str(entry.get("term", "")).strip()
                if not term or term.lower() in seen:
                    continue
                seen.add(term.lower())
                related.append({"term": term,
                                "part_of_speech": str(entry.get("part_of_speech") or ""),
                                "translation_uk": str(entry.get("translation_uk") or ""),
                                "note_uk": str(entry.get("note_uk") or "")})
            item[key] = related
        items.append(item)
    lesson["items"] = items
    if lesson.get("level") not in LEVELS:
        # Якщо модель не вказала рівень уроку, беремо найчастіший рівень серед слів
        levels = [item["level"] for item in items]
        lesson["level"] = max(set(levels), key=levels.count) if levels else "B1"
    if lesson.get("lesson_type") not in ("grammar", "vocabulary"):
        lesson["lesson_type"] = "grammar" if sections else "vocabulary"
    return lesson
