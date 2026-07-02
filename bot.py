import os
import tempfile
import json
import logging
import time
from dotenv import load_dotenv
import telebot
from telebot import types
import fitz  # PyMuPDF
from docx import Document
from groq import Groq

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise SystemExit("Missing TELEGRAM_TOKEN or GROQ_API_KEY in .env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("mcq_bot")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")
client = Groq(api_key=GROQ_API_KEY)

user_data = {}

def extract_text(file_path: str, ext: str) -> str:
    if ext == ".pdf":
        with fitz.open(file_path) as doc:
            text = "\n".join(page.get_text("text") for page in doc)
    elif ext == ".docx":
        doc = Document(file_path)
        text = "\n".join(p.text for p in doc.paragraphs)
    else:  # txt
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    return text.strip()

def generate_mcqs(text: str, num_questions: int, difficulty: str):
    prompt = f"""Create exactly {num_questions} {difficulty}-level MCQs based on the text.
Return ONLY valid JSON:

{{
  "questions": [
    {{
      "question": "Question text?",
      "options": ["A. Option1", "B. Option2", "C. Option3", "D. Option4"],
      "correct": "A",
      "explanation": "Short explanation."
    }}
  ]
}}

Text: {text[:13000]}"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=4000,
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)

@bot.message_handler(commands=['start', 'help'])
def start(message):
    markup = types.InlineKeyboardMarkup(row_width=2)
    for n in [5, 10, 15, 20]:
        markup.add(types.InlineKeyboardButton(f"{n} Questions", callback_data=f"q_{n}"))
    
    bot.reply_to(message, "👋 *MCQ Generator Bot*\n\nChoose how many questions you want:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("q_"))
def num_handler(call):
    num = int(call.data.split("_")[1])
    user_data[call.message.chat.id] = {"num": num, "diff": "medium"}
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"✅ *{num} questions* selected.\n\nNow send a **PDF**, **DOCX**, or **TXT** file.",
        call.message.chat.id, call.message.message_id
    )

@bot.message_handler(content_types=['document'])
def handle_doc(message):
    doc = message.document
    ext = os.path.splitext(doc.file_name.lower())[1]
    if ext not in {".pdf", ".docx", ".txt"}:
        return bot.reply_to(message, "❌ Only PDF, DOCX, and TXT files are supported.")

    state = user_data.get(message.chat.id, {"num": 5, "diff": "medium"})
    num = state["num"]
    diff = state["diff"]

    bot.reply_to(message, f"🔄 Generating {num} {diff} MCQs...")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        local_path = tmp.name
        file_data = bot.download_file(bot.get_file(doc.file_id).file_path)
        with open(local_path, 'wb') as f:
            f.write(file_data)

    text = extract_text(local_path, ext)
    os.unlink(local_path)

    if len(text) < 100:
        return bot.reply_to(message, "❌ Not enough text found in the file.")

    try:
        data = generate_mcqs(text, num, diff)

        reply = f"📝 **{num} {diff.capitalize()} MCQs Generated**\n\n"
        for i, q in enumerate(data.get("questions", []), 1):
            reply += f"**Q{i}. {q.get('question')}**\n\n"
            for opt in q.get("options", []):
                reply += f"{opt}\n"
            reply += f"\n✅ **Correct Answer: {q.get('correct')}**\n"
            reply += f"💡 {q.get('explanation', '')}\n\n"
            reply += "━━━━━━━━━━━━━━━━━━━━━━\n\n"

        # Split if too long
        if len(reply) > 4000:
            for chunk in [reply[i:i+4000] for i in range(0, len(reply), 4000)]:
                bot.reply_to(message, chunk)
        else:
            bot.reply_to(message, reply)

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

if __name__ == "__main__":
    print("🚀 MCQ Bot is Running...")
    bot.infinity_polling()
