"""Показує моделі, доступні для вашого ключа. Запуск: python models.py"""

from dotenv import load_dotenv

load_dotenv()

from coach.llm import LlmError, best_model, list_models, model_name, provider  # noqa: E402

if __name__ == "__main__":
    print(f"Провайдер: {provider()}   Поточна модель: {model_name()}\n")
    try:
        names = list_models()
    except LlmError as error:
        print(f"Помилка: {error}")
    else:
        if not names:
            print("Список моделей доступний лише для Gemini.")
        best = best_model(names)
        for name in names:
            print("  ", name, "  ← рекомендована" if name == best else "")
        if best:
            print(f"\nВпишіть у файл .env:  COACH_MODEL={best}")
