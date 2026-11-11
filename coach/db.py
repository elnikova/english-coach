"""База даних SQLite: матеріали, словник, інтервальне повторення."""

import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

DEFAULT_CATEGORIES = [
    "Code review", "Stand-up & meetings", "Interview", "Debugging",
    "Architecture", "DevOps", "Documentation", "Everyday", "Business", "Other",
]
FALLBACK_CATEGORY = "Other"  # сюди переносимо слова з видаленої категорії

DATA = Path(__file__).resolve().parent.parent / "data"
DB = DATA / "coach.db"
UPLOADS = DATA / "uploads"

SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,          -- text | image | pdf | file | files
    source_text TEXT DEFAULT '',
    file_name TEXT DEFAULT '',
    lesson TEXT NOT NULL,               -- JSON уроку, який згенерував агент
    notes TEXT DEFAULT '',              -- власні нотатки користувача
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    position INTEGER DEFAULT 100
);
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL UNIQUE COLLATE NOCASE,
    transcription TEXT DEFAULT '',
    part_of_speech TEXT DEFAULT '',
    forms TEXT DEFAULT '',              -- основні форми слова
    translation TEXT DEFAULT '',
    definition TEXT DEFAULT '',
    level TEXT DEFAULT 'B1',
    category TEXT DEFAULT 'Other',
    examples TEXT DEFAULT '[]',         -- JSON: [{"en": ..., "uk": ...}]
    family TEXT DEFAULT '[]',           -- JSON: споріднені слова інших частин мови
    material_id INTEGER,
    reps INTEGER DEFAULT 0,             -- скільки разів поспіль згадано
    interval_days REAL DEFAULT 0,
    ease REAL DEFAULT 2.5,
    due TEXT DEFAULT (date('now', 'localtime')),
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
"""


def connect() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    UPLOADS.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with closing(connect()) as conn, conn:
        conn.executescript(SCHEMA)
        # Міграція для баз, створених до появи колонки forms
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(words)")}
        if "forms" not in columns:
            conn.execute("ALTER TABLE words ADD COLUMN forms TEXT DEFAULT ''")
        if "family" not in columns:
            conn.execute("ALTER TABLE words ADD COLUMN family TEXT DEFAULT '[]'")
        material_columns = {row["name"] for row in conn.execute("PRAGMA table_info(materials)")}
        if "notes" not in material_columns:
            conn.execute("ALTER TABLE materials ADD COLUMN notes TEXT DEFAULT ''")
        # Перший запуск: наповнюємо список категорій типовими значеннями
        if not conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
            conn.executemany("INSERT INTO categories (name, position) VALUES (?,?)",
                             [(name, i) for i, name in enumerate(DEFAULT_CATEGORIES)])


def query(sql: str, params: tuple = ()) -> list[dict]:
    with closing(connect()) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def execute(sql: str, params: tuple = ()) -> int:
    with closing(connect()) as conn, conn:
        return conn.execute(sql, params).lastrowid


# ---------- Матеріали ----------

def add_material(title, source_type, source_text, file_name, lesson: dict) -> int:
    return execute(
        "INSERT INTO materials (title, source_type, source_text, file_name, lesson) VALUES (?,?,?,?,?)",
        (title, source_type, source_text, file_name, json.dumps(lesson, ensure_ascii=False)),
    )


def _material(row: dict) -> dict:
    row["lesson"] = json.loads(row["lesson"])
    return row


def list_materials() -> list[dict]:
    return [_material(r) for r in query("SELECT * FROM materials ORDER BY id DESC")]


def get_material(material_id: int) -> dict | None:
    rows = query("SELECT * FROM materials WHERE id = ?", (material_id,))
    return _material(rows[0]) if rows else None


def set_material_notes(material_id: int, notes: str) -> bool:
    if not query("SELECT 1 FROM materials WHERE id = ?", (material_id,)):
        return False
    execute("UPDATE materials SET notes = ? WHERE id = ?", (notes, material_id))
    return True


# ---------- Категорії ----------

def category_names() -> list[str]:
    return [row["name"] for row in
            query("SELECT name FROM categories ORDER BY position, name COLLATE NOCASE")]


def list_categories() -> list[dict]:
    return query(
        "SELECT c.id, c.name, COUNT(w.id) AS words FROM categories c"
        " LEFT JOIN words w ON w.category = c.name COLLATE NOCASE"
        " GROUP BY c.id, c.name ORDER BY c.position, c.name COLLATE NOCASE")


def add_category(name: str) -> int:
    position = query("SELECT COALESCE(MAX(position), 0) + 1 AS n FROM categories")[0]["n"]
    return execute("INSERT INTO categories (name, position) VALUES (?,?)", (name, position))


def rename_category(category_id: int, name: str) -> bool:
    rows = query("SELECT name FROM categories WHERE id = ?", (category_id,))
    if not rows:
        return False
    execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
    execute("UPDATE words SET category = ? WHERE category = ? COLLATE NOCASE", (name, rows[0]["name"]))
    return True


def delete_category(category_id: int) -> bool:
    """Видаляє категорію; слова з неї переносимо в запасну."""
    rows = query("SELECT name FROM categories WHERE id = ?", (category_id,))
    if not rows or rows[0]["name"].lower() == FALLBACK_CATEGORY.lower():
        return False
    if not query("SELECT 1 FROM categories WHERE name = ? COLLATE NOCASE", (FALLBACK_CATEGORY,)):
        add_category(FALLBACK_CATEGORY)
    execute("UPDATE words SET category = ? WHERE category = ? COLLATE NOCASE",
            (FALLBACK_CATEGORY, rows[0]["name"]))
    execute("DELETE FROM categories WHERE id = ?", (category_id,))
    return True


def set_material_level(material_id: int, level: str) -> None:
    material = get_material(material_id)
    if material:
        material["lesson"]["level"] = level
        execute("UPDATE materials SET lesson = ? WHERE id = ?",
                (json.dumps(material["lesson"], ensure_ascii=False), material_id))


# ---------- Словник ----------

_ARTICLES = ("a ", "an ", "the ", "to ")


def normalize_term(term: str) -> str:
    """Ключ для пошуку дублікатів: без регістру, артиклів, дефісів і розділових знаків."""
    text = " ".join(str(term).lower().split())
    text = text.replace("\u2019", "'").replace("-", " ").strip(" .,!?;:\"'")
    for article in _ARTICLES:
        if text.startswith(article):
            text = text[len(article):]
            break
    return " ".join(text.split())


def find_word(term: str) -> dict | None:
    """Шукає картку з таким самим словом, навіть якщо написання трохи інше."""
    key = normalize_term(term)
    if not key:
        return None
    rows = query("SELECT * FROM words WHERE term = ? COLLATE NOCASE", (str(term).strip(),))
    if rows:
        return _word(rows[0])
    for row in query("SELECT * FROM words"):
        if normalize_term(row["term"]) == key:
            return _word(row)
    return None


def _word(row: dict) -> dict:
    row["examples"] = json.loads(row["examples"] or "[]")
    row["family"] = json.loads(row.get("family") or "[]")
    return row


def list_words() -> list[dict]:
    return [_word(r) for r in query("SELECT * FROM words ORDER BY id DESC")]


def add_word(item: dict, material_id: int | None = None) -> int | None:
    """Додає картку; повертає None, якщо таке слово вже є."""
    term = (item.get("term") or "").strip()
    if not term or find_word(term):
        return None
    return execute(
        "INSERT INTO words (term, transcription, part_of_speech, forms, translation, definition,"
        " level, category, examples, family, material_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            term,
            item.get("transcription", ""),
            item.get("part_of_speech", ""),
            item.get("forms", ""),
            item.get("translation_uk", ""),
            item.get("definition_en", ""),
            item.get("level", "B1"),
            item.get("category", "Other"),
            json.dumps(item.get("examples", []), ensure_ascii=False),
            json.dumps(item.get("family", []), ensure_ascii=False),
            material_id,
        ),
    )


def add_example(word_id: int, en: str, uk: str) -> list | None:
    """Додає власний приклад до картки; повертає оновлений список."""
    rows = query("SELECT examples FROM words WHERE id = ?", (word_id,))
    if not rows:
        return None
    examples = json.loads(rows[0]["examples"] or "[]")
    examples.append({"en": en, "uk": uk, "tense": "", "form": "", "own": True})
    execute("UPDATE words SET examples = ? WHERE id = ?",
            (json.dumps(examples, ensure_ascii=False), word_id))
    return examples


def delete_example(word_id: int, index: int) -> list | None:
    rows = query("SELECT examples FROM words WHERE id = ?", (word_id,))
    if not rows:
        return None
    examples = json.loads(rows[0]["examples"] or "[]")
    if not 0 <= index < len(examples):
        return None
    examples.pop(index)
    execute("UPDATE words SET examples = ? WHERE id = ?",
            (json.dumps(examples, ensure_ascii=False), word_id))
    return examples


def due_words() -> list[dict]:
    today = date.today().isoformat()
    return [_word(r) for r in query("SELECT * FROM words WHERE due <= ? ORDER BY due, id", (today,))]


def grade_word(word_id: int, grade: int) -> None:
    """Спрощений SM-2. grade: 0 не знаю, 1 важко, 2 знаю, 3 легко."""
    rows = query("SELECT reps, interval_days, ease FROM words WHERE id = ?", (word_id,))
    if not rows:
        return
    reps, interval, ease = rows[0]["reps"], rows[0]["interval_days"], rows[0]["ease"]
    if grade == 0:
        reps, interval, ease = 0, 0, max(1.3, ease - 0.2)
    else:
        if reps == 0:
            interval = 1
        elif reps == 1:
            interval = 3
        else:
            interval = interval * ease
        if grade == 1:
            interval, ease = max(1, interval * 0.6), max(1.3, ease - 0.15)
        elif grade == 3:
            interval, ease = interval * 1.3, ease + 0.15
        reps += 1
    due = (date.today() + timedelta(days=round(interval))).isoformat()
    execute(
        "UPDATE words SET reps = ?, interval_days = ?, ease = ?, due = ? WHERE id = ?",
        (reps, interval, ease, due, word_id),
    )
