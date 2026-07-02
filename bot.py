"""
Telegram MCQ Generator Bot
Upload a PDF / DOCX / TXT -> get back an MCQ quiz PDF generated via Groq (Llama 3.3).

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
from fpdf import FPDF
from groq import Groq
from dotenv import load_dotenv



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


@dataclass
class ChatState:
    num_questions: int = 5
    difficulty: str = "medium"
    busy: bool = False 

chat_states: dict[int, ChatState] = {}


def get_state(chat_id: int) -> ChatState:
    return chat_states.setdefault(chat_id, ChatState())


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
      "options": ["A. Option1", "B. Option2", "C. Option3", "D. Option4"],
      "correct": "A",
      "explanation": "One sentence explanation."
    }}
  ]
}}

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
# PDF rendering
# --------------------------------------------------------------------------- #

def _safe(text: str) -> str:
    """FPDF's core fonts are Latin-1 only; degrade unsupported characters cleanly."""
    return text.encode("latin-1", "replace").decode("latin-1")


class QuizPDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(30, 30, 30)
        self.cell(0, 10, _safe(self.title), ln=1, align="C")
        self.set_draw_color(200, 200, 200)
        self.line(10, 20, 200, 20)
        self.ln(6)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def build_pdf(data: dict, num_questions: int, difficulty: str, chat_id: int) -> str:
    pdf = QuizPDF()
    pdf.title = f"MCQ Quiz  -  {difficulty.capitalize()} Level  -  {num_questions} Questions"
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    for i, q in enumerate(data.get("questions", []), 1):
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(20, 20, 20)
        pdf.multi_cell(0, 8, _safe(f"Q{i}. {q.get('question', '')}"))
        pdf.ln(1)

        pdf.set_font("Helvetica", "", 11)
        correct_letter = str(q.get("correct", "")).strip()[:1].upper()
        for opt in q.get("options", []):
            is_correct = opt.strip().upper().startswith(correct_letter)
            if is_correct:
                pdf.set_text_color(0, 110, 0)
                pdf.set_font("Helvetica", "B", 11)
            else:
                pdf.set_text_color(60, 60, 60)
                pdf.set_font("Helvetica", "", 11)
            pdf.multi_cell(0, 7, _safe(f"   {opt}"))

        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(90, 90, 90)
        explanation = q.get("explanation", "")
        if explanation:
            pdf.multi_cell(0, 6, _safe(f"Explanation: {explanation}"))
        pdf.ln(5)

    pdf_path = os.path.join(tempfile.gettempdir(), f"mcqs_{chat_id}_{int(time.time())}.pdf")
    pdf.output(pdf_path)
    return pdf_path


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
        "Send me a PDF, DOCX or TXT file and I'll turn it into a multiple-choice quiz.\n\n"
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

    state.busy = True
    status_msg = bot.reply_to(message, f"📥 Reading your file...")

    local_path = None
    pdf_path = None
    try:
        bot.send_chat_action(chat_id, "typing")
        file_info = bot.get_file(doc.file_id)
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

        bot.edit_message_text("📄 Building your quiz PDF...", chat_id, status_msg.message_id)
        pdf_path = build_pdf(data, state.num_questions, state.difficulty, chat_id)

        with open(pdf_path, "rb") as f:
            bot.send_document(
                chat_id, f,
                caption=f"✅ Your {state.num_questions}-question {state.difficulty} quiz is ready!",
            )
        bot.delete_message(chat_id, status_msg.message_id)

    except Exception as e:  # noqa: BLE001
        log.exception("Failed to process document for chat %s", chat_id)
        bot.edit_message_text(f"❌ Something went wrong: {e}", chat_id, status_msg.message_id)
    finally:
        state.busy = False
        for p in (local_path, pdf_path):
            if p and os.path.exists(p):
                os.unlink(p)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    bot.reply_to(message, "Send /start to begin, then upload a PDF, DOCX or TXT file. 🙂")


if __name__ == "__main__":
    log.info("🚀 MCQ Bot running...")
    bot.infinity_polling()
