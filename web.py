"""English Coach — адмінка в браузері. Запуск: python web.py"""

import base64
import json
import os
import re
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from coach import db  # noqa: E402
from coach.agent import EnglishCoach  # noqa: E402
from coach.quiz import build_quiz  # noqa: E402
from coach.tutor import LEVELS, build_card, build_lesson, categories, detect_kind  # noqa: E402

# 127.0.0.1 — лише цей комп'ютер. 0.0.0.0 — ще й інші ваші пристрої через Tailscale.
HOST = (os.getenv("COACH_HOST") or "127.0.0.1").strip()
PORT = int((os.getenv("COACH_PORT") or "8000").strip())
PAGE = Path(__file__).parent / "web" / "index.html"
MAX_BODY = 14 * 1024 * 1024  # приблизно 10 МБ файлів у base64
MAX_FILES = 8

coach: EnglishCoach | None = None
chat_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------- Обробники API ----------

def chat_start(_):
    global coach
    with chat_lock:
        coach = EnglishCoach()
        return {"reply": coach.start()}


def chat_send(data):
    global coach
    text = str(data.get("text", "")).strip()
    if not text:
        raise ApiError(400, "Empty message")
    with chat_lock:
        if coach is None:
            coach = EnglishCoach()
        return {"reply": coach.reply(text)}


def material_create(data):
    text = str(data.get("text", "")).strip()
    files = [f for f in (data.get("files") or []) if isinstance(f, dict) and f.get("data")]
    if len(files) > MAX_FILES:
        raise ApiError(400, f"Too many files: {MAX_FILES} at most")
    if not text and not files:
        raise ApiError(400, "Add some text or a file")
    kinds = [detect_kind(f) for f in files]
    kind = "text" if not files else kinds[0] if len(files) == 1 else "files"
    lesson = build_lesson(text=text, files=files, max_items=10 if len(files) < 2 else 15)

    # Якщо з матеріалу не вийшов повноцінний урок, просто поповнюємо словник
    if not lesson["sections"] and not lesson["exercises"]:
        added, skipped = [], []
        for item in lesson["items"]:
            (added if db.add_word(item) else skipped).append(item["term"])
        return {"kind": "words", "added": added, "skipped": skipped}

    names = []
    for index, file in enumerate(files):
        name = re.sub(r"[^\w.\-]+", "_", file.get("name") or "file")
        (db.UPLOADS / f"{int(time.time())}_{index}_{name}").write_bytes(base64.b64decode(file["data"]))
        names.append(name)
    title = str(data.get("title", "")).strip() or lesson["title"]
    material_id = db.add_material(title, kind, text, ", ".join(names), lesson)
    return {"kind": "lesson", "material": db.get_material(material_id)}


def category_list(_=None):
    return {"categories": db.list_categories(), "fallback": db.FALLBACK_CATEGORY}


def _category_name(data) -> str:
    name = " ".join(str(data.get("name", "")).split())[:40]
    if not name:
        raise ApiError(400, "Enter a category name")
    return name


def category_create(data):
    name = _category_name(data)
    if db.query("SELECT 1 FROM categories WHERE name = ? COLLATE NOCASE", (name,)):
        raise ApiError(409, f"Category “{name}” already exists")
    db.add_category(name)
    return category_list()


def category_rename(category_id, data):
    name = _category_name(data)
    clash = db.query("SELECT id FROM categories WHERE name = ? COLLATE NOCASE", (name,))
    if clash and clash[0]["id"] != category_id:
        raise ApiError(409, f"Category “{name}” already exists")
    if not db.rename_category(category_id, name):
        raise ApiError(404, "Category not found")
    return category_list()


def category_delete(category_id):
    if not db.delete_category(category_id):
        raise ApiError(400, f"“{db.FALLBACK_CATEGORY}” cannot be deleted")
    return category_list()


# ---------- Колекції ----------

def collection_list(_=None):
    return {"collections": db.list_collections()}


def collection_create(data):
    name = " ".join(str(data.get("name", "")).split())[:40]
    if not name:
        raise ApiError(400, "Enter a collection name")
    if db.query("SELECT 1 FROM collections WHERE name = ? COLLATE NOCASE", (name,)):
        raise ApiError(409, f"Collection “{name}” already exists")
    created = db.add_collection(name)
    return {"collections": db.list_collections(), "created": created}


def collection_rename(collection_id, data):
    name = " ".join(str(data.get("name", "")).split())[:40]
    if not name:
        raise ApiError(400, "Enter a collection name")
    clash = db.query("SELECT id FROM collections WHERE name = ? COLLATE NOCASE", (name,))
    if clash and clash[0]["id"] != collection_id:
        raise ApiError(409, f"Collection “{name}” already exists")
    if not db.rename_collection(collection_id, name):
        raise ApiError(404, "Collection not found")
    return collection_list()


def collection_delete(collection_id):
    if not db.delete_collection(collection_id):
        raise ApiError(404, "Collection not found")
    return collection_list()


def word_collection(word_id, data):
    try:
        collection_id = int(data.get("collection_id", 0))
    except (TypeError, ValueError):
        raise ApiError(400, "Unknown collection") from None
    if not db.set_word_collection(word_id, collection_id, bool(data.get("on"))):
        raise ApiError(404, "Word or collection not found")
    return {"ok": True}


def material_update(material_id, data):
    changed = False
    if "level" in data:
        if data["level"] not in LEVELS:
            raise ApiError(400, "Unknown level")
        db.set_material_level(material_id, data["level"])
        changed = True
    if "notes" in data:
        if not db.set_material_notes(material_id, str(data["notes"])[:5000]):
            raise ApiError(404, "Material not found")
        changed = True
    if not changed:
        raise ApiError(400, "Nothing to update")
    return {"ok": True}


def add_related_words(item: dict) -> list[str]:
    """Споріднені слова й синоніми одразу стають короткими картками."""
    added = []
    for entry in list(item.get("family") or []) + list(item.get("synonyms") or []):
        if db.add_light_word(entry, item.get("level", "B1"), item.get("category", "Other")):
            added.append(entry["term"])
    return added


def word_enrich(word_id, _=None):
    """Доповнює коротку картку: транскрипція, приклади, споріднені слова, синоніми."""
    rows = db.query("SELECT term FROM words WHERE id = ?", (word_id,))
    if not rows:
        raise ApiError(404, "Word not found")
    item = build_card(rows[0]["term"])
    if not item:
        raise ApiError(500, "Could not complete the card")
    db.complete_word(word_id, item)
    return {"ok": True, "added": add_related_words(item)}


def example_add(word_id, data):
    en = " ".join(str(data.get("en", "")).split())[:300]
    uk = " ".join(str(data.get("uk", "")).split())[:300]
    if not en:
        raise ApiError(400, "Enter an example sentence in English")
    examples = db.add_example(word_id, en, uk)
    if examples is None:
        raise ApiError(404, "Word not found")
    return {"examples": examples}


def example_delete(word_id, index):
    examples = db.delete_example(word_id, index)
    if examples is None:
        raise ApiError(404, "Example not found")
    return {"examples": examples}


def material_add_words(data):
    material = db.get_material(int(data.get("material_id", 0)))
    if not material:
        raise ApiError(404, "Material not found")
    items = material["lesson"]["items"]
    indexes = data.get("indexes")
    chosen = items if indexes is None else [items[i] for i in indexes if 0 <= i < len(items)]
    added = sum(1 for item in chosen if db.add_word(item, material["id"]))
    return {"added": added, "skipped": len(chosen) - added}


def word_create(data):
    term = str(data.get("term", "")).strip()
    if not term:
        raise ApiError(400, "Enter a word or phrase")
    # Якщо таке слово вже є, нічого не питаємо в моделі — просто показуємо наявну картку
    existing = db.find_word(term)
    if existing:
        return {"ok": True, "term": existing["term"], "existed": True}
    item = build_card(term)
    if not item:
        raise ApiError(500, "Could not create the card")
    existing = db.find_word(item["term"])
    if existing:
        return {"ok": True, "term": existing["term"], "existed": True}
    db.add_word(item)
    return {"ok": True, "term": item["term"], "existed": False, "added": add_related_words(item)}


def word_update(word_id, data):
    if data.get("category") in categories():
        db.execute("UPDATE words SET category = ? WHERE id = ?", (data["category"], word_id))
    if data.get("level") in LEVELS:
        db.execute("UPDATE words SET level = ? WHERE id = ?", (data["level"], word_id))
    if "note" in data:
        db.execute("UPDATE words SET note = ? WHERE id = ?", (str(data["note"])[:2000], word_id))
    return {"ok": True}


def review_grade(word_id, data):
    db.grade_word(word_id, int(data.get("grade", 0)))
    return {"ok": True}


def stats(_=None):
    return {
        "words": db.query("SELECT COUNT(*) AS n FROM words")[0]["n"],
        "materials": db.query("SELECT COUNT(*) AS n FROM materials")[0]["n"],
        "due": len(db.due_words()),
        "categories": categories(),
        "collections": db.list_collections(),
        "levels": LEVELS,
    }


def quiz(params):
    try:
        return {"questions": build_quiz(db.list_words(), params.get("kind", "choice"))}
    except ValueError as error:
        raise ApiError(400, str(error)) from None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path, _, raw_query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in raw_query.split("&") if "=" in p)
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        self._api(lambda: self._get(path, params))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "Files are too large (10 MB in total)"})
        raw = self.rfile.read(length)
        self._api(lambda: self._post(self.path, json.loads(raw or b"{}")))

    def do_DELETE(self):
        self._api(lambda: self._delete(self.path))

    def _get(self, path, params):
        if path == "/api/categories":
            return category_list()
        if path == "/api/collections":
            return collection_list()
        if path == "/api/stats":
            return stats()
        if path == "/api/materials":
            return {"materials": db.list_materials()}
        if path == "/api/words":
            return {"words": db.list_words()}
        if path == "/api/review":
            collection = params.get("collection")
            return {"words": db.due_words(int(collection) if (collection or "").isdigit() else None)}
        if path == "/api/quiz":
            return quiz(params)
        raise ApiError(404, "Not found")

    def _post(self, path, data):
        simple = {
            "/api/start": chat_start,
            "/api/chat": chat_send,
            "/api/materials": material_create,
            "/api/words": word_create,
            "/api/words/from-material": material_add_words,
            "/api/categories": category_create,
            "/api/collections": collection_create,
        }
        if path in simple:
            return simple[path](data)
        if match := re.fullmatch(r"/api/words/(\d+)/examples", path):
            return example_add(int(match[1]), data)
        if match := re.fullmatch(r"/api/words/(\d+)/enrich", path):
            return word_enrich(int(match[1]))
        if match := re.fullmatch(r"/api/words/(\d+)/collections", path):
            return word_collection(int(match[1]), data)
        if match := re.fullmatch(r"/api/collections/(\d+)", path):
            return collection_rename(int(match[1]), data)
        if match := re.fullmatch(r"/api/categories/(\d+)", path):
            return category_rename(int(match[1]), data)
        if match := re.fullmatch(r"/api/materials/(\d+)", path):
            return material_update(int(match[1]), data)
        if match := re.fullmatch(r"/api/words/(\d+)", path):
            return word_update(int(match[1]), data)
        if match := re.fullmatch(r"/api/review/(\d+)", path):
            return review_grade(int(match[1]), data)
        raise ApiError(404, "Not found")

    def _delete(self, path):
        if match := re.fullmatch(r"/api/words/(\d+)/examples/(\d+)", path):
            return example_delete(int(match[1]), int(match[2]))
        if match := re.fullmatch(r"/api/collections/(\d+)", path):
            return collection_delete(int(match[1]))
        if match := re.fullmatch(r"/api/categories/(\d+)", path):
            return category_delete(int(match[1]))
        if match := re.fullmatch(r"/api/words/(\d+)", path):
            db.delete_word(int(match[1]))
            return {"ok": True}
        if match := re.fullmatch(r"/api/materials/(\d+)", path):
            db.execute("DELETE FROM materials WHERE id = ?", (int(match[1]),))
            return {"ok": True}
        raise ApiError(404, "Not found")

    def _api(self, action):
        try:
            self._json(200, action())
        except ApiError as error:
            self._json(error.status, {"error": str(error)})
        except ValueError as error:
            self._json(400, {"error": str(error)})
        except Exception as error:
            self._json(500, {"error": str(error)})

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # не засмічуємо термінал логами запитів
        pass


def main():
    db.init()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"English Coach is running at {url}  (stop: Ctrl+C)")
    if HOST == "0.0.0.0":
        name = socket.gethostname().lower()
        print(f"From your other devices (Tailscale): http://{name}:{PORT}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye! See you next session.")


if __name__ == "__main__":
    main()
