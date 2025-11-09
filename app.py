import os
import textwrap
import asyncio
import logging
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
    PicklePersistence,
    ExtBot,
)
from flask import Flask, request
from google import genai

# =================================================================
# 1. КОНСТАНТЫ И НАСТРОЙКИ
# =================================================================

# Эти значения будут взяты из Environment Variables на Render
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY") 

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-2.5-flash"
SYSTEM_INSTRUCTION = """
Ты - персональный Тренер и Диетолог, твой никнейм – FIT AI. Твоя главная задача – помочь пользователю достичь его целей в фитнесе, используя научный, безопасный и мотивирующий подход.
[... СИСТЕМНАЯ ИНСТРУКЦИЯ ИДЕТ ДАЛЬШЕ ...]
"""
# Настройка Gemini с синхронным клиентом
client = genai.Client(api_key=GEMINI_API_KEY)


# =================================================================
# 2. ИНИЦИАЛИЗАЦИЯ FLASK И PTB
# =================================================================

flask_app = Flask(__name__)

# Создание Application
persistence = PicklePersistence(filepath="fit_ai_persistence")

application = Application.builder().token(TELEGRAM_TOKEN).updater(None).arbitrary_callback_data(True).persistence(persistence).build()
application.initialize() # Инициализация для асинхронного роута


# =================================================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (Логика сессий)
# =================================================================

def get_chat_session(chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Получает или создает сессию Gemini для чата (СИНХРОННО)."""
    SESSION_KEY = 'gemini_session'

    if SESSION_KEY not in context.chat_data:
        logger.info(f"[{chat_id}] Создание новой сессии Gemini...")
        chat = client.chats.create(
            model=MODEL_NAME,
            config={'system_instruction': SYSTEM_INSTRUCTION}
        )
        context.chat_data[SESSION_KEY] = chat

    return context.chat_data[SESSION_KEY]


# =================================================================
# 4. ФУНКЦИИ PTB (Обработчики сообщений) - АСИНХРОННЫЕ
# =================================================================

async def start_or_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик для /start и /reset (АСИНХРОННЫЙ)."""
    chat_id = update.effective_chat.id
    
    # Сброс сессии
    if 'gemini_session' in context.chat_data:
        del context.chat_data['gemini_session']
        logger.info(f"[{chat_id}] Сессия Gemini сброшена.")

    chat_session = get_chat_session(chat_id, context)
    
    await update.message.reply_text(
        "👋 Привет! Я твой **FIT AI**. Я помогу тебе с фитнесом и питанием. Для начала, расскажи о своих **целях**, **ограничениях** (если есть) и **месте тренировок**.", 
        parse_mode='Markdown'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Основной обработчик сообщений (АСИНХРОННЫЙ)."""
    user_text = update.message.text
    chat_id = update.effective_chat.id

    chat_session = get_chat_session(chat_id, context)

    await update.message.chat.send_action('typing')
    
    try:
        # !!! ИСПОЛЬЗУЕМ to_thread для запуска синхронного клиента Gemini !!!
        # Это гарантирует, что главный поток Webhook не будет заблокирован
        response = await asyncio.to_thread(chat_session.send_message, user_text)
        final_answer = response.text
        
        # Разбиваем ответ на части и отправляем
        chunks = textwrap.wrap(final_answer, 4000, replace_whitespace=False)
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode='Markdown')
            
    except Exception as e:
        logger.error(f"[{chat_id}] Критическая ошибка Gemini/Telegram: {e}")
        error_message = f"Произошла критическая ошибка: {e}"
        await update.message.reply_text(error_message)


# Добавление обработчиков
application.add_handler(CommandHandler("start", start_or_reset))
application.add_handler(CommandHandler("reset", start_or_reset))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


# =================================================================
# 5. ФУНКЦИИ FLASK (Web App) - АСИНХРОННЫЙ РОУТ
# =================================================================

@flask_app.route('/', methods=['GET'])
def index():
    """Главный роут для проверки, что Web App работает."""
    return 'FIT AI Webhook is running!', 200

# Роут для Telegram (АСИНХРОННЫЙ)
@flask_app.route('/webhook', methods=['POST'])
async def webhook(): 
    """Принимает JSON-обновление от Telegram."""
    if request.method == "POST":
        # process_update теперь асинхронный и должен вызываться через await
        await application.process_update(
            Update.de_json(request.get_json(force=True), application.bot)
        )
        return "ok"
    return "Error: Method not allowed", 405

# Роут для установки Webhook - ИСПРАВЛЕН
@flask_app.route('/set_webhook', methods=['GET'])
async def set_webhook():
    """Установка вебхука (АСИНХРОННАЯ)."""
    # Render предоставляет имя хоста в переменной RENDER_EXTERNAL_HOSTNAME
    HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not HOSTNAME:
        # В случае локального тестирования или отсутствия переменной
        return "Ошибка: Переменная RENDER_EXTERNAL_HOSTNAME не найдена.", 500
        
    WEBHOOK_URL = f"https://{HOSTNAME}/webhook"
    
    try:
        # Устанавливаем полный URL для Webhook
        await application.bot.set_webhook(url=WEBHOOK_URL)
        return "Webhook установлен успешно!", 200
    except Exception as e:
        logger.error(f"Ошибка при установке Webhook: {e}")
        # Выводим ошибку Telegram, чтобы понимать, что не так
        return f"Ошибка Telegram API: {e}", 500


# Псевдоним для Gunicorn/uWSGI
application_pa = flask_app
