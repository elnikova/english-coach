"""Додає слова до словника або будує уроки з тем, узятих зі списку.

Застосунок має бути запущений в іншому вікні терміналу (python web.py).

    python add_words.py lists/words.txt              # картки у словник
    python add_words.py lists/lessons.txt --lessons  # уроки

Формат списку слів — один рядок на слово; після тире можна дописати значення,
в якому слово вам трапилося, і модель візьме саме цей відтінок:

    errand — доручення

Формат списку тем — блоки, розділені порожнім рядком; кожен блок іде агенту
як матеріал для одного уроку.

Рядки, що починаються з #, пропускаються. Скрипт можна запускати повторно:
слова, які вже є у словнику, він не чіпає.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DASHES = re.compile(r"\s+[—–-]\s+")  # довге тире, середнє тире або дефіс з пробілами


def call(url: str, payload: dict | None = None, timeout: int = 420) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def read_blocks(path: Path, as_lessons: bool) -> list[str]:
    lines = [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("#")]
    if not as_lessons:
        return [line.strip() for line in lines if line.strip()]
    blocks, current = [], []
    for line in lines + [""]:
        if line.strip():
            current.append(line.strip())
        elif current:
            blocks.append(" ".join(current))
            current = []
    return blocks


def split_hint(line: str) -> tuple[str, str]:
    """'errand — доручення' -> ('errand', 'доручення')"""
    parts = DASHES.split(line, maxsplit=1)
    return (parts[0].strip(), parts[1].strip() if len(parts) > 1 else "")


def send_word(base: str, line: str) -> str:
    term, hint = split_hint(line)
    if not term:
        return "порожній рядок"
    result = call(f"{base}/api/words", {"term": term, "hint": hint})
    if result.get("existed"):
        return f"{result['term']} — уже у словнику"
    related = result.get("added") or []
    tail = f", разом із ним: {', '.join(related)}" if related else ""
    return f"{result['term']}{tail}"


def send_lesson(base: str, text: str) -> str:
    result = call(f"{base}/api/materials", {"text": text})
    if result.get("kind") == "lesson":
        lesson = result["material"]["lesson"]
        return f"урок «{lesson['title']}», слів: {len(lesson.get('items', []))}"
    return f"уроку не вийшло, до словника додано слів: {len(result.get('added', []))}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="файл зі списком")
    parser.add_argument("--lessons", action="store_true",
                        help="рядки — це теми для уроків, а не слова")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="адреса застосунку")
    parser.add_argument("--pause", type=float, default=12.0,
                        help="пауза між запитами (ліміт Gemini — близько 10 за хвилину)")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        print(f"Файл не знайдено: {path}")
        return 1

    items = read_blocks(path, args.lessons)
    if not items:
        print("У файлі немає жодного рядка.")
        return 1

    try:
        call(f"{args.url}/api/stats")
    except urllib.error.URLError as error:
        print(f"Застосунок не відповідає на {args.url}: {error}")
        print("Запустіть його в іншому вікні: python web.py")
        return 1

    what = "тем" if args.lessons else "слів"
    print(f"У списку {len(items)} {what}.\n")

    failed = []
    for index, item in enumerate(items, 1):
        label = item if len(item) < 60 else item[:57] + "…"
        print(f"[{index}/{len(items)}] {label} … ", end="", flush=True)
        started = time.time()
        try:
            report = send_lesson(args.url, item) if args.lessons else send_word(args.url, item)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            message = json.loads(body).get("error", body) if body.startswith("{") else body
            print(f"помилка: {message}")
            failed.append(label)
        except Exception as error:  # обрив зв'язку, таймаут
            print(f"помилка: {error}")
            failed.append(label)
        else:
            print(f"{report}  ({time.time() - started:.0f} с)")
        if index < len(items):
            time.sleep(args.pause)

    print(f"\nГотово. Опрацьовано: {len(items) - len(failed)}, з помилкою: {len(failed)}.")
    for label in failed:
        print(f"  × {label}")
    if failed:
        print("Запустіть скрипт ще раз — уже додане він пропустить.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
