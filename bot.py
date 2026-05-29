import asyncio
import json
import logging
import os
import random
import re
from pathlib import Path

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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DATA_FILE = Path("questions.json")
TEXT_FILE = Path("questions.txt")

# Replace with your Telegram user ID or set ADMIN_IDS environment variable as comma-separated IDs
ADMIN_IDS = []
raw_admin_ids = os.getenv("ADMIN_IDS", "")
for item in raw_admin_ids.replace(" ", "").split(","):
    if item:
        try:
            ADMIN_IDS.append(int(item))
        except ValueError:
            logger.warning("Неверный ADMIN_IDS в .env: %s", item)
logger.info("Loaded ADMIN_IDS: %s", ADMIN_IDS)

ADD_Q_TEXT, ADD_OPT_A, ADD_OPT_B, ADD_OPT_C, ADD_OPT_D, ADD_ANSWER, ADD_EXPLANATION, ADD_LECTURE_ID = range(8)
EDIT_ID, EDIT_Q_TEXT, EDIT_OPT_A, EDIT_OPT_B, EDIT_OPT_C, EDIT_OPT_D, EDIT_ANSWER, EDIT_EXPLANATION = range(8, 16)
UPLOAD_TITLE, UPLOAD_VIDEO = range(16, 18)

user_sessions: dict[int, int] = {}
user_current_lecture: dict[int, int] = {}
user_lecture_sessions: dict[int, dict] = {}
user_progress: dict[int, int] = {}

# Вопросы для тестов по лекциям должны быть связаны с конкретными лекциями через поле "lecture_id" в вопросе.
def load_questions() -> list[dict]:
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                questions = json.load(f)
            if isinstance(questions, list) and questions:
                for question in questions:
                    question.setdefault("lecture_id", 1)
                return questions
            logger.warning(
                "questions.json содержит пустой или неверный список; будет выполнена загрузка из questions.txt"
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "Не удалось прочитать questions.json: %s. Попытка загрузки из questions.txt",
                e,
            )

    if not TEXT_FILE.exists():
        raise FileNotFoundError("questions.txt не найден в папке с ботом")

    questions = parse_questions_txt(TEXT_FILE.read_text(encoding="utf-8"))
    save_questions(questions)
    return questions
def load_lectures() -> list[dict]:
    LECTURES_FILE = Path("lectures.json")
    if LECTURES_FILE.exists():
        with LECTURES_FILE.open("r", encoding="utf-8") as f:
            lectures = json.load(f)
        for idx, lecture in enumerate(lectures, start=1):
            lecture.setdefault("id", idx)
            lecture.setdefault("title", f"Лекция {idx}")
        return lectures
    return [
        {"id": 1, "title": "Лекция 1", "file_id": None},
        {"id": 2, "title": "Лекция 2", "file_id": None},
        {"id": 3, "title": "Лекция 3", "file_id": None},
    ]


def save_questions(questions: list[dict]) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(questions, f, ensure_ascii=False, indent=2)


def save_lectures(lectures: list[dict]) -> None:
    with Path("lectures.json").open("w", encoding="utf-8") as f:
        json.dump(lectures, f, ensure_ascii=False, indent=2)


async def import_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    if not TEXT_FILE.exists():
        await update.message.reply_text("Файл questions.txt не найден.")
        return

    questions = parse_questions_txt(TEXT_FILE.read_text(encoding="utf-8"))
    save_questions(questions)
    await update.message.reply_text(f"Импортировано {len(questions)} вопросов из questions.txt.")


def parse_questions_txt(text: str) -> list[dict]:
    blocks = [block.strip() for block in re.split(r"-{3,}", text) if block.strip()]
    questions = []
    current_lecture_id = 1

    for idx, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        lecture_id = current_lecture_id
        if lines and re.match(r"^(?:Лекция|Lecture)\s+(\d+)(?:\.?|:)?$", lines[0], flags=re.IGNORECASE):
            lecture_id = int(re.match(r"^(?:Лекция|Lecture)\s+(\d+)(?:\.?|:)?$", lines[0], flags=re.IGNORECASE).group(1))
            current_lecture_id = lecture_id
            lines = lines[1:]

        if lines and re.match(r"^Вопрос\s*\d+\.?$", lines[0], flags=re.IGNORECASE):
            lines = lines[1:]

        if len(lines) < 6:
            raise ValueError(f"Неверный формат блока вопроса: {block[:100]}")

        question_text = lines[0]
        option_lines = lines[1:-1]
        answer_line = lines[-1]

        options = []
        for line in option_lines:
            match = re.match(r"^([АБВГA-D])\)\s*(.*)$", line, flags=re.IGNORECASE)
            if not match:
                raise ValueError(f"Неверный формат варианта ответа: {line}")
            label = match.group(1).upper()
            options.append({"label": label, "text": match.group(2).strip()})

        answer_match = re.match(r"^\d+\s+([АБВГA-D])\s+(.*)$", answer_line, flags=re.IGNORECASE)
        if not answer_match:
            raise ValueError(f"Неверный формат строки ответа: {answer_line}")

        answer_label = answer_match.group(1).upper()
        explanation = answer_match.group(2).strip()

        questions.append(
            {
                "id": idx,
                "text": question_text,
                "options": options,
                "answer": answer_label,
                "explanation": explanation,
                "lecture_id": lecture_id,
            }
        )

    return questions


def format_question(question: dict) -> str:
    lines = [f"{question['text']}\n"]
    for option in question["options"]:
        lines.append(f"{option['label']}) {option['text']}")
    return "\n".join(lines)


def build_options_keyboard(question: dict, include_next: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    for option in question["options"]:
        buttons.append(
            [InlineKeyboardButton(option["label"], callback_data=option["label"])]
        )
    if include_next:
        buttons.append([InlineKeyboardButton("Еще вопрос", callback_data="NEXT")])
    return InlineKeyboardMarkup(buttons)


def get_question_by_id(question_id: int, questions: list[dict]) -> dict | None:
    return next((q for q in questions if q["id"] == question_id), None)


def get_lecture_by_id(lecture_id: int, lectures: list[dict]) -> dict | None:
    return next((l for l in lectures if l["id"] == lecture_id), None)


def get_questions_for_lecture(lecture_id: int, questions: list[dict]) -> list[dict]:
    return [q for q in questions if q.get("lecture_id", 1) == lecture_id]


def build_lectures_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    lectures = load_lectures()
    progress = user_progress.get(user_id, 0) if user_id is not None else 0
    buttons = []
    for lecture in lectures:
        if lecture.get("id", 0) <= progress + 1:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{lecture['id']}. {lecture['title']}",
                        callback_data=f"LECTURE_{lecture['id']}",
                    )
                ]
            )
        else:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"🔒 {lecture['id']}. {lecture['title']}",
                        callback_data=f"LOCKED_{lecture['id']}",
                    )
                ]
            )
    return InlineKeyboardMarkup(buttons) if buttons else InlineKeyboardMarkup([[InlineKeyboardButton("Нет лекций", callback_data="NONE")]])


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🛡️Добро пожаловать в «Безопасный шаг»!\n\n"
        "Здесь вы научитесь распознавать уловки мошенников и защищать себя в цифровом мире.\n\n"
        "📓Изучайте теорию - короткие понятные уроки.\n"
        "🎯Применяйте знания на практике - отвечайте на вопросы.\n\n"
        "Готовы сделать первый шаг к безопасности? Жмите на кнопку ниже и начинайте обучение!\n\n"
        "❓Задать вопрос можно тут - @SafeStepTicketsbot"
    )
    if is_admin(update.effective_user.id):
        text += "\nАдмин-команды:\n/add_question - добавить вопрос\n/edit_question - редактировать вопрос\n/list_questions - показать список вопросов\n/upload_lecture - загрузить видео-лекцию\n/import_questions - импортировать тесты из questions.txt\n"

    reply_markup = build_lectures_keyboard(update.effective_user.id)
    with open("start_image.jpg", "rb") as img:
        await update.message.reply_photo(photo=img,
                                        caption=text,
                                        reply_markup=reply_markup)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    questions = load_questions()
    question = random.choice(questions)
    user_sessions[update.effective_user.id] = question["id"]
    await update.message.reply_text(
        format_question(question),
        reply_markup=build_options_keyboard(question),
    )
async def start_lectures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lectures = load_lectures()
    if not lectures:
        await update.message.reply_text("Видео-лекции пока не добавлены.")
        return

    await update.message.reply_text(
        "Выберите доступную лекцию:",
        reply_markup=build_lectures_keyboard(update.effective_user.id),
    )


async def start_lecture(update: Update, context: ContextTypes.DEFAULT_TYPE, lecture_id: int) -> None:
    lectures = load_lectures()
    lecture = get_lecture_by_id(lecture_id, lectures)
    if not lecture:
        await update.effective_chat.send_message("Лекция не найдена.")
        return

    progress = user_progress.get(update.effective_user.id, 0)
    if lecture_id > progress + 1:
        await update.effective_chat.send_message(
            "Эта лекция пока закрыта. Пройдите предыдущую лекцию."
        )
        return

    if lecture.get("file_id"):
        await update.effective_chat.send_message(
            f"Открываю лекцию {lecture_id}: {lecture['title']}"
        )
        await context.bot.send_video(
            chat_id=update.effective_chat.id,
            video=lecture["file_id"],
            caption=lecture.get("title", ""),
        )
    else:
        await update.effective_chat.send_message(
            "Для этой лекции еще не загружено видео, но вы можете пройти тест."
        )

    lecture_questions = sorted(
        get_questions_for_lecture(lecture_id, load_questions()),
        key=lambda q: q["id"],
    )
    if not lecture_questions:
        await update.effective_chat.send_message(
            "Для этой лекции пока не настроены вопросы."
        )
        return

    lecture_question_ids = [q["id"] for q in lecture_questions[:15]]
    user_lecture_sessions[update.effective_user.id] = {
        "lecture_id": lecture_id,
        "question_ids": lecture_question_ids,
        "index": 0,
        "all_correct": True,
    }
    first_question = get_question_by_id(lecture_question_ids[0], lecture_questions)
    user_sessions[update.effective_user.id] = first_question["id"]
    user_current_lecture[update.effective_user.id] = lecture_id

    await update.effective_chat.send_message(
        format_question(first_question),
        reply_markup=build_options_keyboard(first_question, include_next=False),
    )


async def list_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    questions = load_questions()
    lines = [f"{q['id']}: {q['text']}" for q in questions]
    text = "Список вопросов:\n" + "\n".join(lines)
    if len(text) > 4000:
        text = "Слишком много вопросов для одного сообщения."
    await update.message.reply_text(text)


async def add_question_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return ConversationHandler.END

    context.user_data["new_question"] = {"options": []}
    await update.message.reply_text("Введите текст нового вопроса.")
    return ADD_Q_TEXT


async def add_question_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_question"]["text"] = update.message.text.strip()
    await update.message.reply_text("Введите вариант ответа А)")
    return ADD_OPT_A


async def add_question_opt_a(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_question"]["options"].append({"label": "А", "text": update.message.text.strip()})
    await update.message.reply_text("Введите вариант ответа Б)")
    return ADD_OPT_B


async def add_question_opt_b(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_question"]["options"].append({"label": "Б", "text": update.message.text.strip()})
    await update.message.reply_text("Введите вариант ответа В)")
    return ADD_OPT_C


async def add_question_opt_c(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_question"]["options"].append({"label": "В", "text": update.message.text.strip()})
    await update.message.reply_text("Введите вариант ответа Г)")
    return ADD_OPT_D


async def add_question_opt_d(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_question"]["options"].append({"label": "Г", "text": update.message.text.strip()})
    await update.message.reply_text("Введите правильную букву ответа (А, Б, В или Г).")
    return ADD_ANSWER


async def add_question_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.message.text.strip().upper()
    if answer not in {"А", "Б", "В", "Г"}:
        await update.message.reply_text("Пожалуйста, введите одну из букв: А, Б, В или Г.")
        return ADD_ANSWER

    context.user_data["new_question"]["answer"] = answer
    await update.message.reply_text("Введите краткое объяснение правильного ответа.")
    return ADD_EXPLANATION


async def add_question_explanation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_question = context.user_data["new_question"]
    new_question["explanation"] = update.message.text.strip()

    await update.message.reply_text(
        "Введите номер лекции для этого вопроса (1, 2 или 3)."
    )
    return ADD_LECTURE_ID


async def add_question_lecture_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        lecture_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введите число: 1, 2 или 3.")
        return ADD_LECTURE_ID

    if lecture_id not in {1, 2, 3}:
        await update.message.reply_text("Введите номер лекции 1, 2 или 3.")
        return ADD_LECTURE_ID

    new_question = context.user_data["new_question"]
    new_question["lecture_id"] = lecture_id

    questions = load_questions()
    new_question["id"] = max((q["id"] for q in questions), default=0) + 1
    questions.append(new_question)
    save_questions(questions)

    await update.message.reply_text(
        f"Вопрос добавлен как #{new_question['id']} для лекции {lecture_id}.",
    )
    return ConversationHandler.END


async def edit_question_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return ConversationHandler.END

    await update.message.reply_text("Введите ID вопроса для редактирования.")
    return EDIT_ID


async def edit_question_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        question_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("ID должен быть числом. Попробуйте снова.")
        return EDIT_ID

    questions = load_questions()
    question = get_question_by_id(question_id, questions)
    if not question:
        await update.message.reply_text("Вопрос с таким ID не найден. Введите другой ID.")
        return EDIT_ID

    context.user_data["edit_question"] = question.copy()
    context.user_data["edit_question"]["options"] = [opt.copy() for opt in question["options"]]

    await update.message.reply_text(
        "Текущий текст вопроса:\n"
        f"{question['text']}\n\n"
        "Введите новый текст вопроса или /skip, чтобы оставить прежний."
    )
    return EDIT_Q_TEXT


async def edit_question_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text != "/skip":
        context.user_data["edit_question"]["text"] = text
    await update.message.reply_text(
        "Введите новый текст варианта ответа А) или /skip."
    )
    return EDIT_OPT_A


async def edit_question_opt_a(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text != "/skip":
        context.user_data["edit_question"]["options"][0]["text"] = text
    await update.message.reply_text("Введите новый текст варианта ответа Б) или /skip.")
    return EDIT_OPT_B


async def edit_question_opt_b(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text != "/skip":
        context.user_data["edit_question"]["options"][1]["text"] = text
    await update.message.reply_text("Введите новый текст варианта ответа В) или /skip.")
    return EDIT_OPT_C


async def edit_question_opt_c(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text != "/skip":
        context.user_data["edit_question"]["options"][2]["text"] = text
    await update.message.reply_text("Введите новый текст варианта ответа Г) или /skip.")
    return EDIT_OPT_D


async def edit_question_opt_d(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text != "/skip":
        context.user_data["edit_question"]["options"][3]["text"] = text
    await update.message.reply_text(
        "Введите новую правильную букву (А, Б, В или Г) или /skip, чтобы оставить прежний ответ."
    )
    return EDIT_ANSWER


async def edit_question_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().upper()
    if text != "/SKIP":
        if text not in {"А", "Б", "В", "Г"}:
            await update.message.reply_text("Пожалуйста, введите одну из букв: А, Б, В, Г или /skip.")
            return EDIT_ANSWER
        context.user_data["edit_question"]["answer"] = text
    await update.message.reply_text("Введите новое объяснение правильного ответа или /skip.")
    return EDIT_EXPLANATION


async def edit_question_explanation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text != "/skip":
        context.user_data["edit_question"]["explanation"] = text

    edited = context.user_data["edit_question"]
    questions = load_questions()
    for idx, q in enumerate(questions):
        if q["id"] == edited["id"]:
            questions[idx] = edited
            break
    save_questions(questions)

    await update.message.reply_text(f"Вопрос #{edited['id']} обновлен.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END


async def upload_lecture_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return ConversationHandler.END

    await update.message.reply_text("Введите название лекции.")
    return UPLOAD_TITLE


async def upload_lecture_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return ConversationHandler.END

    context.user_data["lecture_title"] = update.message.text.strip()
    await update.message.reply_text("Теперь отправьте видео-файл (MP4).")
    return UPLOAD_VIDEO


async def upload_lecture_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    media = update.message.video or update.message.document
    if not media:
        await update.message.reply_text("Пожалуйста, отправьте видео-файл.")
        return UPLOAD_VIDEO

    if update.message.document:
        if not update.message.document.file_name.lower().endswith(".mp4") and not (update.message.document.mime_type or "").startswith("video"):
            await update.message.reply_text("Пожалуйста, отправьте файл MP4 или видео.")
            return UPLOAD_VIDEO

    file_id = media.file_id
    title = context.user_data["lecture_title"]

    lectures = load_lectures()
    next_id = max((lecture.get("id", 0) for lecture in lectures), default=0) + 1
    lectures.append({"id": next_id, "title": title, "file_id": file_id})
    save_lectures(lectures)

    await update.message.reply_text(f"Лекция '{title}' загружена и сохранена как лекция {next_id}.")
    return ConversationHandler.END


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "quiz":
        await quiz(update, context)
        return

    if data == "lectures":
        lectures = load_lectures()
        if not lectures:
            await query.message.reply_text("Видео-лекции пока не добавлены.")
            return

        await query.message.reply_text(
            "Выберите доступную лекцию:",
            reply_markup=build_lectures_keyboard(user_id),
        )
        return

    if data.startswith("LECTURE_"):
        lecture_id = int(data.split("_", 1)[1])
        await start_lecture(update, context, lecture_id)
        return

    if data.startswith("LOCKED_"):
        await query.message.reply_text(
            "Эта лекция пока закрыта. Пройдите предыдущую лекцию.")
        return

    if data == "NONE":
        await query.message.reply_text("Пока нет загруженных лекций.")
        return

    if data == "NEXT":
        questions = load_questions()
        question = random.choice(questions)
        user_sessions[user_id] = question["id"]
        await query.message.reply_text(
            format_question(question),
            reply_markup=build_options_keyboard(question),
        )
        return

    if data in {"А", "Б", "В", "Г"}:
        lecture_session = user_lecture_sessions.get(user_id)
        if lecture_session:
            questions = load_questions()
            question_ids = lecture_session["question_ids"]
            index = lecture_session["index"]
            current_question_id = question_ids[index]
            question = get_question_by_id(current_question_id, questions)
            if question is None:
                await query.message.reply_text("Вопрос не найден. Попробуйте снова.")
                return

            correct = data == question["answer"]
            if not correct:
                lecture_session["all_correct"] = False
            reply = "✅ Верно!\n" if correct else f"❌ Неправильно. Верный ответ: {question['answer']}\n"
            reply += f"\n{question['explanation']}"
            await query.message.reply_text(reply)

            index += 1
            if index < len(question_ids):
                lecture_session["index"] = index
                next_question = get_question_by_id(question_ids[index], questions)
                if next_question is None:
                    await query.message.reply_text("Не удалось загрузить следующий вопрос.")
                    user_lecture_sessions.pop(user_id, None)
                    user_current_lecture.pop(user_id, None)
                    user_sessions.pop(user_id, None)
                    return

                user_sessions[user_id] = next_question["id"]
                await query.message.reply_text(
                    "Следующий вопрос:\n\n" + format_question(next_question),
                    reply_markup=build_options_keyboard(next_question, include_next=False),
                )
                return

            # конец квиза по лекции
            lecture_id = lecture_session["lecture_id"]
            passed_all = lecture_session["all_correct"]
            user_lecture_sessions.pop(user_id, None)
            user_current_lecture.pop(user_id, None)
            user_sessions.pop(user_id, None)

            if passed_all:
                user_progress[user_id] = max(user_progress.get(user_id, 0), lecture_id)
                lectures = load_lectures()
                if lecture_id < len(lectures):
                    await query.message.reply_text(
                        "Поздравляем! Вы ответили правильно на все вопросы. Следующая лекция открыта.",
                        reply_markup=build_lectures_keyboard(user_id),
                    )
                else:
                    await query.message.reply_text(
                        "Поздравляю! Вы успешно прошли все лекции.")
            else:
                await query.message.reply_text(
                    "Вы ответили неверно на один или несколько вопросов. Повторите лекцию еще раз, чтобы открыть следующую.",
                )
            return

        current_question_id = user_sessions.get(user_id)
        current_lecture_id = user_current_lecture.get(user_id)
        if current_question_id is None or current_lecture_id is None:
            await query.message.reply_text(
                "Сначала выберите лекцию, чтобы пройти тест.")
            return

        questions = load_questions()
        question = get_question_by_id(current_question_id, questions)
        if question is None:
            await query.message.reply_text("Вопрос не найден. Попробуйте снова.")
            return

        correct = data == question["answer"]
        reply = "✅ Верно!\n" if correct else f"❌ Неправильно. Верный ответ: {question['answer']}\n"
        reply += f"\n{question['explanation']}"
        await query.message.reply_text(reply)

        if correct:
            user_progress[user_id] = max(user_progress.get(user_id, 0), current_lecture_id)
            user_current_lecture.pop(user_id, None)
            lectures = load_lectures()
            if current_lecture_id < len(lectures):
                await query.message.reply_text(
                    "Вы прошли тест. Следующая лекция открыта.",
                    reply_markup=build_lectures_keyboard(user_id),
                )
            else:
                await query.message.reply_text(
                    "Поздравляю! Вы успешно прошли все лекции.")
        else:
            await query.message.reply_text("Попробуйте ещё раз.")
            await query.message.reply_text(
                format_question(question),
                reply_markup=build_options_keyboard(question),
            )
        return

    await query.message.reply_text("Неизвестная команда. Выберите лекцию или отправьте /help.")


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Не задан токен бота. Установите переменную окружения TELEGRAM_TOKEN.")

    application = ApplicationBuilder().token(token).build()

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add_question", add_question_start)],
        states={
            ADD_Q_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_question_text)],
            ADD_OPT_A: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_question_opt_a)],
            ADD_OPT_B: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_question_opt_b)],
            ADD_OPT_C: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_question_opt_c)],
            ADD_OPT_D: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_question_opt_d)],
            ADD_ANSWER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_question_answer)],
            ADD_EXPLANATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_question_explanation)],
            ADD_LECTURE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_question_lecture_id)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit_question", edit_question_start)],
        states={
            EDIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_question_id)],
            EDIT_Q_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_question_text)],
            EDIT_OPT_A: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_question_opt_a)],
            EDIT_OPT_B: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_question_opt_b)],
            EDIT_OPT_C: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_question_opt_c)],
            EDIT_OPT_D: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_question_opt_d)],
            EDIT_ANSWER: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_question_answer)],
            EDIT_EXPLANATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_question_explanation)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    upload_conv = ConversationHandler(
        entry_points=[CommandHandler("upload_lecture", upload_lecture_start)],
        states={
            UPLOAD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, upload_lecture_title)],
            UPLOAD_VIDEO: [MessageHandler(filters.VIDEO, upload_lecture_video)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("quiz", quiz))
    application.add_handler(CommandHandler("list_questions", list_questions))
    application.add_handler(CommandHandler("start_lectures", start_lectures))
    application.add_handler(CommandHandler("import_questions", import_questions))
    application.add_handler(add_conv)
    application.add_handler(edit_conv)
    application.add_handler(upload_conv)
    application.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Бот запускается...")
    application.run_polling()


if __name__ == "__main__":
    main()
