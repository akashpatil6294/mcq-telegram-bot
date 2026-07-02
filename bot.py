import os
import tempfile
import json
from dotenv import load_dotenv
import telebot
from telebot import types
import fitz
from groq import Groq

load_dotenv()

TELEGRAM_TOKEN = "8840382763:AAGTPmY5-swbXIXg6fBKGfH2LHHzi4sRBkk"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Groq(api_key=GROQ_API_KEY)

user_data = {}

def extract_text_from_pdf(file_path):
    doc = fitz.open(file_path)
    text = "\n".join(page.get_text("text") for page in doc)
    doc.close()
    return text.strip()

def generate_mcqs(text, num_questions=5):
    prompt = f"""Create exactly {num_questions} high-quality MCQs. Return only valid JSON.

{{
  "questions": [
    {{
      "question": "Question text?",
      "options": ["A. Option 1", "B. Option 2", "C. Option 3", "D. Option 4"],
      "correct": "A",
      "explanation": "Short explanation"
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
    return response.choices[0].message.content

def send_long_message(chat_id, text):
    """Split long messages"""
    if len(text) <= 4000:
        bot.send_message(chat_id, text, parse_mode="Markdown")
    else:
        for i in range(0, len(text), 4000):
            bot.send_message(chat_id, text[i:i+4000], parse_mode="Markdown")

@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("5 Questions", callback_data="num_5"))
    markup.add(types.InlineKeyboardButton("10 Questions", callback_data="num_10"))
    markup.add(types.InlineKeyboardButton("15 Questions", callback_data="num_15"))
    
    bot.reply_to(message, "👋 **MCQ Generator**\n\nChoose number of questions:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("num_"))
def callback_handler(call):
    num = int(call.data.split("_")[1])
    user_data[call.message.chat.id] = num
    bot.answer_callback_query(call.id, f"Selected {num} questions")
    bot.send_message(call.message.chat.id, f"✅ **{num} questions** selected.\n\nNow send a PDF or TXT file.")

@bot.message_handler(content_types=['document'])
def handle_doc(message):
    doc = message.document
    if doc.file_size > 10*1024*1024:
        return bot.reply_to(message, "❌ File too large (max 10MB)")

    num_questions = user_data.get(message.chat.id, 5)

    file_info = bot.get_file(doc.file_id)
    ext = os.path.splitext(doc.file_name.lower())[1]

    bot.reply_to(message, f"🔄 Generating **{num_questions} MCQs**...")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        local_path = tmp.name
        file_data = bot.download_file(file_info.file_path)
        with open(local_path, 'wb') as f:
            f.write(file_data)

    text = extract_text_from_pdf(local_path) if ext == '.pdf' else open(local_path, encoding='utf-8').read()
    os.unlink(local_path)

    if len(text) < 100:
        return bot.reply_to(message, "❌ Not enough text found.")

    try:
        result = generate_mcqs(text, num_questions)
        data = json.loads(result)

        reply = f"📝 **{num_questions} MCQs Generated**\n\n"
        for i, q in enumerate(data.get("questions", []), 1):
            reply += f"**Q{i}. {q.get('question')}**\n\n"
            for opt in q.get("options", []):
                reply += f"{opt}\n"
            reply += f"\n✅ **Correct: {q.get('correct')}**\n"
            reply += f"💡 {q.get('explanation', '')}\n\n"
            reply += "━━━━━━━━━━━━━━━━━━━━━━\n\n"

        send_long_message(message.chat.id, reply)
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

if __name__ == "__main__":
    print("🚀 MCQ Bot is Running...")
    bot.infinity_polling()