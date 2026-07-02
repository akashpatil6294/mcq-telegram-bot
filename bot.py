"""
Telegram MCQ Generator Bot
Upload a PDF / DOCX / TXT -> get back an interactive MCQ quiz, right inside the chat.
No files, no downloads - everything happens as native Telegram messages & quiz polls.

Changes in this version:
- PDF generation removed entirely (no fpdf, no QuizPDF, no build_pdf). Nothing is
  written to disk except the temp copy of the uploaded file, which is deleted after use.
- Quizzes are delivered as native Telegram *quiz polls* (bot.send_poll, type="quiz").
  This is a big upgrade over a wall of text: each question becomes a tappable poll,
  Telegram auto-checks the answer for the user, shows correct/incorrect instantly,
  and reveals the explanation as the poll's built-in explanation text.
- After the poll set, a compact text "Answer Key" is posted as a normal chat message
  (fully in-text, nothing to download) so the person can review everything at a glance.
- Long question/option/explanation text is safely trimmed to Telegram's poll limits
  (question <= 300 chars, each option <= 100 chars, explanation <= 200 chars) instead
  of letting the API call fail outright.
- Small UX polish: a live progress message that's edited step by step, a "Retry last
  quiz" inline button after delivery, and a short score-tips footer.
- Kept: state machine per chat, difficulty/question-count selection, retries + JSON
  validation on the Groq call, smart sentence-boundary truncation, rate limiting via
  a per-chat busy flag, /cancel and /settings, logging instead of print().
"""

import os
import re
import json
import time
import logging
import tempfile
from dataclasses import dataclass, field

import telebot
from telebot import types
import fitz  # PyMuPDF
from docx import Document
from groq import Groq
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise SystemExit(
        "Missing TELEGRAM_TOKEN or GROQ_API_KEY. Add them to your .env file before starting the bot."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("mcq_bot")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")
client = Groq(api_key=GROQ_API_KEY)

MODEL = "llama-3.3-70b-versatile"
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB
MAX_TEXT_CHARS = 13000
ALLOWED_EXT = {".pdf", ".docx", ".txt"}
DIFFICULTIES = ["easy", "medium", "hard"]
QUESTION_COUNTS = [5, 10, 15, 20]

# Telegram native poll limits (as of Bot API) - kept as constants so trimming logic
# below has a single source of truth if the platform ever changes them.
POLL_QUESTION_LIMIT = 300
POLL_OPTION_LIMIT = 100
POLL_EXPLANATION_LIMIT = 200


@dataclass
class ChatState:
    num_questions: int = 5
    difficulty: str = "medium"
    busy: bool = False  # simple per-chat lock so one user can't fire two jobs at once
    last_file_id: str | None = None  # lets "Retry" re-run without re-uploading
    last_file_name: str | None = None


chat_states: dict[int, ChatState] = {}


def get_state(chat_id: int) -> ChatState:
    return chat_states.setdefault(chat_id, ChatState())


def trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #

def extract_text(file_path: str, ext: str) -> str:
    if ext == ".pdf":
        with fitz.open(file_path) as doc:
            text = "\n".join(page.get_text("text") for page in doc)
    elif ext == ".docx":
        doc = Document(file_path)
        text = "\n".join(p.text for p in doc.paragraphs)
    else:  # .txt
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    return text.strip()


def smart_truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Cut on a sentence boundary near `limit` instead of mid-word/mid-sentence."""
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(". "), window.rfind("\n"))
    return window[: cut + 1] if cut > limit * 0.5 else window


# --------------------------------------------------------------------------- #
# MCQ generation (Groq)
# --------------------------------------------------------------------------- #

def generate_mcqs(text: str, num_questions: int, difficulty: str, retries: int = 2) -> dict:
    prompt = f"""Create exactly {num_questions} {difficulty}-level multiple choice questions based ONLY on the text below.
Return ONLY valid JSON, no commentary, matching this exact schema:

{{
  "questions": [
    {{
      "question": "Question text?",
      "options": ["Option1", "Option2", "Option3", "Option4"],
      "correct": "A",
      "explanation": "One sentence explanation."
    }}
  ]
}}

Rules:
- "options" must have exactly 4 items, in plain text with NO leading "A./B./C./D." labels.
- "correct" must be the single letter (A, B, C or D) of the correct option's position.
- Keep each question under 250 characters and each option under 90 characters.

Text:
{smart_truncate(text)}"""

    last_err = None
    for attempt in range(1, retries + 2):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=4000,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            questions = data.get("questions", [])
            if not questions:
                raise ValueError("Model returned zero questions")
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("Groq attempt %d failed: %s", attempt, e)
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"MCQ generation failed after retries: {last_err}")


# --------------------------------------------------------------------------- #
# In-chat quiz delivery (native Telegram quiz polls + text answer key)
# --------------------------------------------------------------------------- #

LETTER_TO_INDEX = {"A": 0, "B": 1, "C": 2, "D": 3}


def send_quiz_polls(chat_id: int, data: dict) -> list[dict]:
    """
    Sends one native Telegram quiz-poll per question. Telegram itself handles
    checking the tapped answer, showing right/wrong, and displaying the
    explanation - no PDF, no download, entirely inside the chat.
    Returns the (possibly trimmed) question list for the answer-key text.
    """
    sent_questions = []
    for q in data.get("questions", []):
        question = trim(q.get("question", "Question"), POLL_QUESTION_LIMIT)
        options = [trim(o, POLL_OPTION_LIMIT) for o in q.get("options", [])][:4]
        while len(options) < 4:
            options.append("N/A")

        letter = str(q.get("correct", "A")).strip()[:1].upper()
        correct_index = LETTER_TO_INDEX.get(letter, 0)
        explanation = trim(q.get("explanation", ""), POLL_EXPLANATION_LIMIT)

        bot.send_poll(
            chat_id,
            question=question,
            options=options,
            type="quiz",
            correct_option_id=correct_index,
            is_anonymous=False,
            explanation=explanation or None,
        )
        sent_questions.append(
            {"question": question, "options": options, "correct_index": correct_index, "explanation": explanation}
        )
        time.sleep(0.3)  # gentle pacing so polls don't arrive in one unreadable burst
    return sent_questions


def build_answer_key_text(sent_questions: list[dict], num_questions: int, difficulty: str) -> str:
    lines = [f"📋 *Answer Key — {difficulty.capitalize()} · {num_questions} Qs*\n"]
    for i, q in enumerate(sent_questions, 1):
        correct_opt = q["options"][q["correct_index"]]
        lines.append(f"*Q{i}.* {q['question']}")
        lines.append(f"✅ *Answer:* {correct_opt}")
        if q["explanation"]:
            lines.append(f"_{q['explanation']}_")
        lines.append("")  # blank line between questions
    return "\n".join(lines).strip()


def retry_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔁 Retry with same file", callback_data="retry"))
    return markup


# --------------------------------------------------------------------------- #
# Bot handlers
# --------------------------------------------------------------------------- #

def questions_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[types.InlineKeyboardButton(f"{n} Qs", callback_data=f"q_{n}") for n in QUESTION_COUNTS])
    return markup


def difficulty_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(*[types.InlineKeyboardButton(d.capitalize(), callback_data=f"d_{d}") for d in DIFFICULTIES])
    return markup


@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "👋 *MCQ Generator Bot*\n\n"
        "Send me a PDF, DOCX or TXT file and I'll turn it into a tappable quiz "
        "right here in the chat — no downloads, no files, just interactive polls.\n\n"
        "First, pick how many questions you want:",
        reply_markup=questions_keyboard(),
    )


@bot.message_handler(commands=["settings"])
def settings(message):
    s = get_state(message.chat.id)
    bot.reply_to(
        message,
        f"⚙️ *Current settings*\n\n"
        f"• Questions: {s.num_questions}\n"
        f"• Difficulty: {s.difficulty.capitalize()}\n\n"
        f"Use /start to change these.",
    )


@bot.message_handler(commands=["cancel"])
def cancel(message):
    s = get_state(message.chat.id)
    s.busy = False
    bot.reply_to(message, "❌ Cancelled. Send /start to begin again.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("q_"))
def num_handler(call):
    num = int(call.data.split("_")[1])
    get_state(call.message.chat.id).num_questions = num
    bot.answer_callback_query(call.id, f"{num} questions selected")
    bot.edit_message_text(
        f"✅ *{num} questions* selected.\n\nNow pick a difficulty:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=difficulty_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("d_"))
def diff_handler(call):
    diff = call.data.split("_")[1]
    get_state(call.message.chat.id).difficulty = diff
    bot.answer_callback_query(call.id, f"{diff.capitalize()} selected")
    bot.edit_message_text(
        f"✅ *{diff.capitalize()}* difficulty selected.\n\n"
        f"📎 Now send me a PDF, DOCX or TXT file to generate your quiz.",
        call.message.chat.id,
        call.message.message_id,
    )


@bot.callback_query_handler(func=lambda call: call.data == "retry")
def retry_handler(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    if not state.last_file_id:
        return bot.answer_callback_query(call.id, "No previous file to retry.", show_alert=True)
    if state.busy:
        return bot.answer_callback_query(call.id, "Still working on a quiz — please wait.", show_alert=True)
    bot.answer_callback_query(call.id, "Regenerating…")
    _run_quiz_job(chat_id, state.last_file_id, state.last_file_name)


@bot.message_handler(content_types=["document"])
def handle_doc(message):
    chat_id = message.chat.id
    state = get_state(chat_id)

    if state.busy:
        return bot.reply_to(message, "⏳ Still working on your last quiz — please wait, or send /cancel.")

    doc = message.document
    ext = os.path.splitext(doc.file_name.lower())[1]

    if ext not in ALLOWED_EXT:
        return bot.reply_to(message, "❌ Unsupported file type. Please send a PDF, DOCX or TXT file.")
    if doc.file_size > MAX_FILE_SIZE:
        return bot.reply_to(message, "❌ File too large (max 15 MB).")

    state.last_file_id = doc.file_id
    state.last_file_name = doc.file_name
    _run_quiz_job(chat_id, doc.file_id, doc.file_name)


def _run_quiz_job(chat_id: int, file_id: str, file_name: str):
    state = get_state(chat_id)
    ext = os.path.splitext(file_name.lower())[1]
    state.busy = True
    status_msg = bot.send_message(chat_id, "📥 Reading your file...")

    local_path = None
    try:
        bot.send_chat_action(chat_id, "typing")
        file_info = bot.get_file(file_id)
        file_data = bot.download_file(file_info.file_path)

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            local_path = tmp.name
            tmp.write(file_data)

        text = extract_text(local_path, ext)
        if len(text) < 100:
            bot.edit_message_text("❌ Couldn't find enough readable text in that file.", chat_id, status_msg.message_id)
            return

        bot.edit_message_text(
            f"🧠 Generating {state.num_questions} {state.difficulty} questions with AI...",
            chat_id, status_msg.message_id,
        )
        data = generate_mcqs(text, state.num_questions, state.difficulty)

        bot.edit_message_text("🎯 Sending your interactive quiz...", chat_id, status_msg.message_id)
        sent_questions = send_quiz_polls(chat_id, data)

        answer_key = build_answer_key_text(sent_questions, state.num_questions, state.difficulty)
        # Telegram messages cap at 4096 chars; split the answer key if needed.
        for chunk_start in range(0, len(answer_key), 4000):
            bot.send_message(chat_id, answer_key[chunk_start:chunk_start + 4000])

        bot.delete_message(chat_id, status_msg.message_id)
        bot.send_message(
            chat_id,
            f"✅ Tap through the {len(sent_questions)} polls above, then check the answer key anytime.",
            reply_markup=retry_keyboard(),
        )

    except Exception as e:  # noqa: BLE001
        log.exception("Failed to process document for chat %s", chat_id)
        bot.edit_message_text(f"❌ Something went wrong: {e}", chat_id, status_msg.message_id)
    finally:
        state.busy = False
        if local_path and os.path.exists(local_path):
            os.unlink(local_path)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    bot.reply_to(message, "Send /start to begin, then upload a PDF, DOCX or TXT file. 🙂")


if __name__ == "__main__":
    log.info("🚀 MCQ Bot running...")
    bot.infinity_polling()
