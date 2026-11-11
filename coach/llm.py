"""Єдиний інтерфейс до мовної моделі: Gemini (безкоштовно) або Anthropic."""

import base64
import json
import os
import re
import time

DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "anthropic": "claude-haiku-5-5"}


# Клієнти кешуємо: SDK закриває з'єднання, коли об'єкт клієнта прибирає
# збирач сміття, тож створювати його на кожен запит не можна.
_clients: dict[str, object] = {}
# Модель, якою фактично скористалися, якщо вказаної в .env немає
_resolved: dict[str, str] = {}


class LlmError(RuntimeError):
    """Помилка виклику моделі з текстом, зрозумілим користувачу."""


def provider() -> str:
    """Провайдер із .env; якщо не вказано — визначаємо за наявним ключем."""
    name = (os.getenv("COACH_PROVIDER") or "").strip().lower()
    if name:
        return name
    if (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip():
        return "gemini"
    if (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        return "anthropic"
    return "gemini"


def model_name() -> str:
    """Модель із .env; якщо вона від іншого провайдера — беремо типову."""
    current, name = provider(), (os.getenv("COACH_MODEL") or "").strip()
    is_claude = name.lower().startswith("claude")
    if name and is_claude == (current == "anthropic"):
        return name
    return DEFAULT_MODELS.get(current, name)


# ---------- Публічні функції ----------

def chat_reply(system: str, history: list[dict]) -> str:
    """history: [{"role": "user"|"assistant", "text": ...}] ⇒ відповідь моделі."""
    name = provider()
    try:
        if name == "gemini":
            return _gemini_chat(system, history)
        if name == "anthropic":
            return _anthropic_chat(system, history)
    except LlmError:
        raise
    except Exception as error:
        raise LlmError(_friendly(error)) from error
    raise LlmError(f"Unknown provider “{name}”. Set gemini or anthropic in your .env file.")


def generate_json(system: str, parts: list[dict], schema: dict, max_tokens: int = 32000) -> dict:
    """parts: [{"text": ...}] або [{"mime": "image/png", "data": <base64>}] ⇒ відповідь за схемою."""
    name = provider()
    try:
        if name == "gemini":
            return _gemini_json(system, parts, schema, max_tokens)
        if name == "anthropic":
            return _anthropic_json(system, parts, schema, max_tokens)
    except LlmError:
        raise
    except Exception as error:
        raise LlmError(_friendly(error)) from error
    raise LlmError(f"Unknown provider “{name}”. Set gemini or anthropic in your .env file.")


def list_models() -> list[str]:
    """Моделі, доступні для цього ключа (лише для Gemini)."""
    if provider() != "gemini":
        return []
    try:
        return _gemini_names(_gemini_client())
    except LlmError:
        raise
    except Exception as error:
        raise LlmError(_friendly(error)) from error


def best_model(names: list[str]) -> str:
    """Найдоречніша модель зі списку."""
    return _pick_model(names)


# ---------- Gemini ----------

def _gemini_client():
    key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not key or key.startswith("AIza..."):
        raise LlmError("GEMINI_API_KEY is not set. Add a key from aistudio.google.com to your .env file.")
    client = _clients.get("gemini:" + key)
    if client is None:
        from google import genai
        client = genai.Client(api_key=key)
        _clients["gemini:" + key] = client
    return client


def _gemini_parts(parts: list[dict]):
    from google.genai import types
    built = []
    for part in parts:
        if "text" in part:
            if part["text"].strip():
                built.append(types.Part(text=part["text"]))
        else:
            built.append(types.Part(inline_data=types.Blob(
                mime_type=part["mime"], data=base64.b64decode(part["data"]))))
    return built


def _gemini_names(client) -> list[str]:
    names = []
    for model in client.models.list():
        actions = getattr(model, "supported_actions", None) or []
        if not actions or "generateContent" in actions:
            names.append((model.name or "").replace("models/", ""))
    return sorted(names)


def _rank(name: str):
    """Чим більший ключ, тим краще модель підходить: flash, не lite, стабільна, новіша."""
    low = name.lower()
    if not low.startswith("gemini") or any(
            word in low for word in ("embedding", "aqa", "tts", "audio", "live", "image", "native")):
        return None
    match = re.search(r"gemini-(\d+)(?:[.-](\d+))?", low)
    version = (int(match.group(1)), int(match.group(2) or 0)) if match else (0, 0)
    return ("flash" in low, "lite" not in low,
            not any(word in low for word in ("preview", "exp", "experimental")), version)


def _pick_model(names: list[str]) -> str:
    ranked = [(rank, name) for name in names if (rank := _rank(name))]
    return max(ranked)[1] if ranked else ""


BUSY_WAITS = (2, 5)  # паузи перед повторними спробами, якщо сервер перевантажений


def _is_missing(text: str) -> bool:
    return "not found" in text or "404" in text or "not supported" in text


def _is_busy(text: str) -> bool:
    """Тимчасовий збій на боці сервісу — має сенс повторити запит."""
    return any(word in text for word in
               ("503", "500", "unavailable", "overloaded", "high demand", "internal error"))


def _available(client, exclude: list[str]) -> str:
    """Найдоречніша доступна модель, окрім перелічених."""
    return _pick_model([n for n in _gemini_names(client) if n not in exclude])


def _gemini_generate(contents, config):
    """Запит до Gemini: добирає доступну модель і перечікує тимчасові збої."""
    client = _gemini_client()
    wanted = model_name()
    model = _resolved.get(wanted, wanted)
    tried = [model]              # моделі, які вже пробували в цьому запиті
    waits = list(BUSY_WAITS)     # паузи, що лишилися для поточної моделі

    while True:
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as error:
            text = str(error).lower()

            if _is_missing(text) and len(tried) == 1:
                picked = _available(client, tried)
                if not picked:
                    raise LlmError(
                        f"Model “{model}” is not available, and there are no other models for this key.") from error
                print(f"Model “{model}” is not available — using “{picked}” instead.")
                print(f"To silence this notice, put this in .env:  COACH_MODEL={picked}")
                _resolved[wanted] = picked
                model = picked
                tried.append(picked)
                continue

            if not _is_busy(text):
                raise

            if waits:
                pause = waits.pop(0)
                print(f"Gemini is busy, retrying in {pause}s…")
                time.sleep(pause)
                continue

            picked = _available(client, tried) if len(tried) < 3 else ""
            if picked:
                print(f"Model “{model}” is overloaded — trying “{picked}”.")
                model = picked
                tried.append(picked)
                waits = list(BUSY_WAITS)
                continue

            raise LlmError(
                "Gemini servers are busy right now. Try again in a minute"
                " or set another model in .env (list them with: python models.py)."
            ) from error


def _gemini_chat(system: str, history: list[dict]) -> str:
    from google.genai import types
    contents = [types.Content(role=("model" if m["role"] == "assistant" else "user"),
                              parts=[types.Part(text=m["text"])]) for m in history]
    response = _gemini_generate(
        contents, types.GenerateContentConfig(system_instruction=system, max_output_tokens=2000))
    text = (response.text or "").strip()
    if not text:
        raise LlmError("The model returned an empty reply. Please try again.")
    return text


def _gemini_json(system: str, parts: list[dict], schema: dict, max_tokens: int) -> dict:
    from google.genai import types
    response = _gemini_generate(
        [types.Content(role="user", parts=_gemini_parts(parts))],
        types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=max_tokens,
        ))
    text = (response.text or "").strip()
    if not text:
        candidates = getattr(response, "candidates", None)
        reason = str(getattr(candidates[0], "finish_reason", "")) if candidates else ""
        if reason.upper().endswith("MAX_TOKENS"):
            raise LlmError("This material is too large for one lesson. Try fewer files at a time.")
        raise LlmError("The model did not return a lesson. Please try again.")
    return _parse_json(text)


# ---------- Anthropic ----------

def _anthropic_client():
    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key.startswith("sk-"):
        raise LlmError("ANTHROPIC_API_KEY is not set. Add a key from console.anthropic.com to your .env file.")
    client = _clients.get("anthropic:" + key)
    if client is None:
        from anthropic import Anthropic
        client = Anthropic(api_key=key)
        _clients["anthropic:" + key] = client
    return client


def _anthropic_chat(system: str, history: list[dict]) -> str:
    client = _anthropic_client()
    response = client.messages.create(
        model=model_name(), max_tokens=1024, system=system,
        messages=[{"role": m["role"], "content": m["text"]} for m in history],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _anthropic_json(system: str, parts: list[dict], schema: dict, max_tokens: int) -> dict:
    content = []
    for part in parts:
        if "text" in part:
            content.append({"type": "text", "text": part["text"]})
        elif part["mime"] == "application/pdf":
            content.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf", "data": part["data"]}})
        else:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": part["mime"], "data": part["data"]}})
    tool = {"name": "save_lesson", "description": "Save the finished lesson.", "input_schema": schema}
    client = _anthropic_client()
    response = client.messages.create(
        model=model_name(), max_tokens=min(max_tokens, 16000), system=system,
        tools=[tool], messages=[{"role": "user", "content": content}],
    )
    for block in response.content:
        if block.type == "tool_use" and isinstance(block.input, dict):
            return dict(block.input)
    if response.stop_reason == "max_tokens":
        raise LlmError("This material is too large for one lesson. Try fewer files at a time.")
    return _parse_json("".join(b.text for b in response.content if b.type == "text"))


# ---------- Спільне ----------

def _parse_json(text: str) -> dict:
    """Беремо JSON із відповіді, навіть якщо він загорнутий у ```json ... ```."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LlmError("The model did not reply in JSON. Please try again.")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as error:
        raise LlmError("Could not read the model reply. Please try again.") from error
    if not isinstance(data, dict):
        raise LlmError("The model replied in an unexpected format.")
    return data


def _friendly(error: Exception) -> str:
    text = str(error)
    low = text.lower()
    if "429" in text or "resource_exhausted" in low or "quota" in low or "rate limit" in low:
        return ("Free request limit reached. Wait a minute and try again"
                " (the Gemini free tier limits requests per minute and per day).")
    if "api key" in low or "api_key" in low or "401" in text or "403" in text or "permission" in low:
        return "The key was rejected. Check the key in your .env file."
    if "503" in text or "unavailable" in low or "overloaded" in low or "high demand" in low:
        return ("Gemini servers are busy right now. Try again in a minute"
                " or set another model in .env (list them with: python models.py).")
    if "not found" in low or "404" in text:
        return (f"Model “{model_name()}” is not available for your key."
                " Run python models.py to see the available models.")
    if "deadline" in low or "timeout" in low or "connection" in low:
        return "No connection to the service. Check your internet and try again."
    return text
