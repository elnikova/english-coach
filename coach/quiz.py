"""Тести зі словника. Генеруються локально, без запитів до моделі."""

import random
import re


def _options(words: list[dict], correct: dict, key: str) -> list[str]:
    pool = list({w[key] for w in words if w[key] and w[key] != correct[key]})
    options = random.sample(pool, 3) + [correct[key]]
    random.shuffle(options)
    return options


def build_quiz(words: list[dict], kind: str, count: int = 10) -> list[dict]:
    """kind: 'choice' — вибір перекладу, 'gap' — пропуск у реченні."""
    usable = [w for w in words if w["translation"]]
    if len({w["translation"] for w in usable}) < 4 or len(usable) < 4:
        raise ValueError("You need at least 4 words in your vocabulary for a quiz.")
    questions = []
    for word in random.sample(usable, len(usable)):
        if len(questions) >= count:
            break
        if kind == "gap":
            # Шукаємо приклад, де слово (або його форма) зустрічається дослівно
            found = None
            for example in random.sample(word["examples"], len(word["examples"])):
                for form in (example.get("form") or "", word["term"]):
                    pattern = re.compile(r"\b" + re.escape(form) + r"\b", re.IGNORECASE) if form else None
                    if pattern and pattern.search(example.get("en", "")):
                        found = (example, form, pattern)
                        break
                if found:
                    break
            if not found:
                continue
            example, form, pattern = found
            others = list({w["term"] for w in usable if w["term"].lower() not in (form.lower(), word["term"].lower())})
            options = random.sample(others, 3) + [form]
            random.shuffle(options)
            questions.append({
                "prompt": pattern.sub("_____", example["en"], count=1),
                "hint": example.get("uk", ""),
                "options": options,
                "answer": form,
            })
        else:
            questions.append({
                "prompt": word["term"],
                "hint": word["transcription"],
                "options": _options(usable, word, "translation"),
                "answer": word["translation"],
            })
    if not questions:
        raise ValueError("Could not build this quiz: the examples have no usable sentences.")
    return questions
