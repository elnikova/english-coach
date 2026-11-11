"""English Coach — консольний діалог. Запуск: python main.py"""

import sys

from dotenv import load_dotenv

load_dotenv()

from coach.agent import EnglishCoach  # noqa: E402

EXIT_WORDS = {"/exit", "/quit", "exit", "quit"}


def main() -> int:
    try:
        coach = EnglishCoach()
        print("English Coach  (exit: /exit)\n")
        print(f"Coach: {coach.start()}\n")
    except Exception as error:
        print(f"Could not start the agent: {error}")
        print("Check the API key in your .env file.")
        return 1

    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in EXIT_WORDS:
            break
        try:
            print(f"\nCoach: {coach.reply(text)}\n")
        except Exception as error:
            print(f"\nRequest error: {error}\n")

    print("Bye! See you next session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
