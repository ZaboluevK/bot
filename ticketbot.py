import json
import logging
import os
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
DOTENV_PATH = BASE_DIR / ".env"

try:
    from dotenv import load_dotenv

    load_dotenv(DOTENV_PATH)
except ImportError:
    def load_dotenv(path: Path) -> None:
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value

    load_dotenv(DOTENV_PATH)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TICKET_DATA_FILE = BASE_DIR / "tickets.json"
ADMIN_IDS = []
raw_admin_ids = os.getenv("ADMIN_IDS", "")
for item in raw_admin_ids.replace(" ", "").split(","):
    if item:
        try:
            ADMIN_IDS.append(int(item))
        except ValueError:
            logger.warning("Неверный ADMIN_IDS в .env: %s", item)

logger.info("Loaded ADMIN_IDS: %s", ADMIN_IDS)

TICKET_TEXT = 0


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def load_tickets() -> list[dict]:
    if TICKET_DATA_FILE.exists():
        try:
            with TICKET_DATA_FILE.open("r", encoding="utf-8") as f:
                tickets = json.load(f)
            if isinstance(tickets, list):
                return tickets
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Не удалось прочитать tickets.json: %s", e)
    return []


def save_tickets(tickets: list[dict]) -> None:
    with TICKET_DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(tickets, f, ensure_ascii=False, indent=2)


def create_ticket(user_id: int, username: str | None, text: str) -> dict:
    tickets = load_tickets()
    ticket_id = max((ticket.get("id", 0) for ticket in tickets), default=0) + 1
    ticket = {
        "id": ticket_id,
        "user_id": user_id,
        "username": username or "(нет имени)",
        "text": text.strip(),
        "status": "open",
    }
    tickets.append(ticket)
    save_tickets(tickets)
    return ticket


def format_ticket(ticket: dict) -> str:
    return (
        f"🎫 Тикет #{ticket['id']}\n"
        f"Пользователь: {ticket['username']}\n"
        f"ID: {ticket['user_id']}\n"
        f"Статус: {ticket['status']}\n"
        f"Текст:\n{ticket['text']}"
    )


def find_ticket(ticket_id: int) -> dict | None:
    tickets = load_tickets()
    return next((ticket for ticket in tickets if ticket.get("id") == ticket_id), None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я бот-помощник для обращения в службу поддержки.\n\n"
        "Отправь /ticket, чтобы создать новый запрос.\n"
        "Администраторы получат твой тикет и смогут ответить с помощью /reply."
    )
    if is_admin(update.effective_user.id):
        text += (
            "\n\nАдмин-команды:\n"
            "/tickets - список тикетов\n"
            "/reply <id> <текст> - ответ пользователю\n"
            "/close <id> - закрыть тикет"
        )
    await update.message.reply_text(text)


async def ticket_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Опишите ваш вопрос или проблему. Я отправлю тикет админам."
    )
    return TICKET_TEXT


async def ticket_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Текст не должен быть пустым. Пожалуйста, опишите проблему.")
        return TICKET_TEXT

    ticket = create_ticket(
        update.effective_user.id,
        update.effective_user.username,
        text,
    )

    admin_message = (
        f"🆕 Новый тикет #{ticket['id']}\n"
        f"Пользователь: {ticket['username']}\n"
        f"ID: {ticket['user_id']}\n\n"
        f"{ticket['text']}\n\n"
        "Используйте /reply {id} <текст> для ответа и /close {id} для закрытия."
    ).replace("{id}", str(ticket['id']))

    if ADMIN_IDS:
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(admin_id, admin_message)
            except Exception as exc:
                logger.warning("Не удалось отправить тикет администратору %s: %s", admin_id, exc)
    else:
        await update.message.reply_text(
            "Тикет создан, но список администраторов пуст. Установите ADMIN_IDS в .env."
        )

    await update.message.reply_text(
        "Спасибо! Ваш тикет отправлен админам."
    )
    return ConversationHandler.END


async def list_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    tickets = load_tickets()
    if not tickets:
        await update.message.reply_text("Тикетов пока нет.")
        return

    lines = [
        f"#{ticket['id']} [{ticket['status']}] {ticket['username']}: {ticket['text'][:50]}"
        for ticket in tickets
    ]
    text = "Список тикетов:\n" + "\n".join(lines)
    await update.message.reply_text(text)


async def reply_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /reply <ticket_id> <текст ответа>")
        return

    try:
        ticket_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID тикета должно быть числом.")
        return

    ticket = find_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text(f"Тикет #{ticket_id} не найден.")
        return

    if ticket["status"] == "closed":
        await update.message.reply_text(f"Тикет #{ticket_id} уже закрыт.")
        return

    answer_text = " ".join(args[1:]).strip()
    if not answer_text:
        await update.message.reply_text("Текст ответа не может быть пустым.")
        return

    try:
        await context.bot.send_message(
            ticket["user_id"],
            (
                f"Ответ на ваш тикет #{ticket_id}:\n\n"
                f"{answer_text}"
            ),
        )
        await update.message.reply_text(f"Ответ отправлен пользователю. Тикет #{ticket_id}.")
    except Exception as exc:
        logger.warning("Не удалось отправить ответ пользователю %s: %s", ticket["user_id"], exc)
        await update.message.reply_text(
            "Не удалось отправить ответ пользователю. Проверьте, что бот может ему писать."
        )


async def close_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Использование: /close <ticket_id>")
        return

    try:
        ticket_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID тикета должно быть числом.")
        return

    tickets = load_tickets()
    for ticket in tickets:
        if ticket.get("id") == ticket_id:
            if ticket["status"] == "closed":
                await update.message.reply_text(f"Тикет #{ticket_id} уже закрыт.")
                return
            ticket["status"] = "closed"
            save_tickets(tickets)
            await update.message.reply_text(f"Тикет #{ticket_id} закрыт.")
            return

    await update.message.reply_text(f"Тикет #{ticket_id} не найден.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END


def main() -> None:
    token = os.getenv("TICKETBOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не задан токен бота. Установите переменную окружения TICKETBOT_TOKEN."
        )

    application = ApplicationBuilder().token(token).build()

    ticket_conv = ConversationHandler(
        entry_points=[CommandHandler("ticket", ticket_start)],
        states={
            TICKET_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ticket_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(ticket_conv)
    application.add_handler(CommandHandler("tickets", list_tickets))
    application.add_handler(CommandHandler("reply", reply_ticket))
    application.add_handler(CommandHandler("close", close_ticket))

    logger.info("Ticket bot запускается...")
    application.run_polling()


if __name__ == "__main__":
    main()
