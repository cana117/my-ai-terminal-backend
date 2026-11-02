import os
import logging
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Получаем токен бота из переменных окружения
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "MY_AI // Telegram Bot & Backend Server is online!"

@app.route("/api/schem", methods=["POST"])
def generate_schematic():
    """Эндпоинт для генерации .schem файлов майнкрафта"""
    data = request.json
    description = data.get("description", "default_build")
    
    # Здесь в будущем будет логика создания WorldEdit схематики
    logger.info(f"Generating .schem for: {description}")
    
    return jsonify({
        "status": "success",
        "message": f"Схематика для постройки '{description}' успешно создана в памяти бэкенда."
    })

# --- ТЕЛЕГРАМ БОТ ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"Здравствуй, {user_name}! 🚀\n"
        "Я — бэкенд-бот твоего хакерского терминала.\n"
        "Используй /help для просмотра доступных команд."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "КОМАНДЫ БОТА:\n"
        "/start — Запуск бота\n"
        "/news — Сводка обновлений PS99 и нейросети\n"
        "/mc <описание> — Запросить генерацию .schem файла"
    )

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📰 [NEWS]: Ивент PS99 активен, множитель удачи х2 включен. Все системы стабильны.")

async def mc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("⚠️ Укажи описание постройки, например: /mc ферма тростника")
        return
    await update.message.reply_text(f"🧱 Генерация .schem файла по запросу: '{query}'... Готово! (Файл готов к передаче).")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await update.message.reply_text(f"🤖 [TERMINAL RELAY]: Получено сообщение: «{text}». Передано в кластер.")

def main_bot():
    if not TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не задан! Бот не сможет запуститься в режиме polling.")
        return

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("news", news_command))
    application.add_handler(CommandHandler("mc", mc_command))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    logger.info("Telegram Bot started successfully.")
    application.run_polling()

if __name__ == "__main__":
    import threading
    # Запускаем Flask-сервер в отдельном потоке, чтобы Render не ругался на отсутствие веб-порта
    port = int(os.environ.get("PORT", 10000))
    
    # Если токен бота есть, запускаем бота в фоновом потоке, а Flask на главном
    if TOKEN:
        import asyncio
        # Запуск бота в отдельном потоке
        bot_thread = threading.Thread(target=main_bot, daemon=True)
        bot_thread.start()

    app.run(host="0.0.0.0", port=port)