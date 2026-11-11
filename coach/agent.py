"""Розмовний коуч: зберігає історію діалогу та звертається до моделі."""

from .llm import chat_reply
from .prompts import OPENING_REQUEST, SYSTEM_PROMPT

MAX_HISTORY = 16  # скільки останніх повідомлень надсилати моделі (більше — дорожчий запит)


class EnglishCoach:
    def __init__(self):
        self.history: list[dict] = []

    def start(self) -> str:
        """Початок сесії: коуч вітається і ставить перше запитання."""
        return self.reply(OPENING_REQUEST)

    def reply(self, user_text: str) -> str:
        """Надсилає репліку учня і повертає відповідь коуча."""
        self.history.append({"role": "user", "text": user_text})
        try:
            answer = chat_reply(SYSTEM_PROMPT, self._window())
        except Exception:
            self.history.pop()  # не залишаємо репліку без відповіді
            raise
        self.history.append({"role": "assistant", "text": answer})
        return answer

    def _window(self) -> list[dict]:
        """Останні повідомлення; вікно має починатися з репліки користувача."""
        window = self.history[-MAX_HISTORY:]
        while window and window[0]["role"] != "user":
            window = window[1:]
        return window
