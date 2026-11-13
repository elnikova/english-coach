"""Додає до бази уроки з папки, яких там ще немає.

Застосунок має бути запущений в іншому вікні терміналу (python web.py).
Скрипт сам дивиться, які файли вже стали уроками, і надсилає лише решту —
по одному, з паузою, щоб не впертися в ліміт запитів Gemini.

    python add_lessons.py "D:\\English\\курси\\Grammar"

Якщо щось не вийшло, просто запустіть ще раз: уже додані уроки пропускаються.
"""

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SUPPORTED = {".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
MIMES = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".gif": "image/gif"}


def safe_name(name: str) -> str:
    """Так застосунок перейменовує файл, коли зберігає його до бази."""
    return re.sub(r"[^\w.\-]+", "_", name)


def call(url: str, payload: dict | None = None, timeout: int = 420) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def existing(base: str) -> set[str]:
    """Назви файлів, з яких уроки вже зроблено."""
    names = set()
    for material in call(f"{base}/api/materials")["materials"]:
        for name in (material.get("file_name") or "").split(","):
            if name.strip():
                names.add(name.strip())
    return names


def order(path: Path):
    """Сортуємо за номером уроку, а не за назвою: 2 має йти перед 10."""
    match = re.search(r"A1[_-](\d+)", path.name)
    return (int(match[1]) if match else 10**6, path.name.lower())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="папка з файлами уроків")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="адреса застосунку")
    parser.add_argument("--pause", type=float, default=12.0,
                        help="пауза між уроками в секундах (ліміт Gemini — близько 10 запитів за хвилину)")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Папки не знайдено: {folder}")
        return 1

    try:
        done = existing(args.url)
    except urllib.error.URLError as error:
        print(f"Застосунок не відповідає на {args.url}: {error}")
        print("Запустіть його в іншому вікні: python web.py")
        return 1

    files = sorted((f for f in folder.iterdir()
                    if f.is_file() and f.suffix.lower() in SUPPORTED), key=order)
    todo = [f for f in files if safe_name(f.name) not in done]

    print(f"У папці: {len(files)} файлів. Уже в базі: {len(files) - len(todo)}. Додаємо: {len(todo)}.")
    if not todo:
        return 0

    failed = []
    for index, path in enumerate(todo, 1):
        print(f"[{index}/{len(todo)}] {path.name} … ", end="", flush=True)
        payload = {"files": [{
            "name": path.name,
            "type": MIMES.get(path.suffix.lower(), "application/octet-stream"),
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        }]}
        started = time.time()
        try:
            result = call(f"{args.url}/api/materials", payload)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            message = json.loads(body).get("error", body) if body.startswith("{") else body
            print(f"помилка: {message}")
            failed.append(path.name)
        except Exception as error:  # обрив зв'язку, таймаут
            print(f"помилка: {error}")
            failed.append(path.name)
        else:
            if result.get("kind") == "lesson":
                lesson = result["material"]["lesson"]
                print(f"урок «{lesson['title']}», слів: {len(lesson.get('items', []))}"
                      f"  ({time.time() - started:.0f} с)")
            else:
                added = len(result.get("added", []))
                print(f"уроку не вийшло, до словника додано слів: {added}")
        if index < len(todo):
            time.sleep(args.pause)

    print(f"\nГотово. Додано: {len(todo) - len(failed)}, з помилкою: {len(failed)}.")
    for name in failed:
        print(f"  × {name}")
    if failed:
        print("Запустіть скрипт ще раз — він спробує лише ці файли.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
