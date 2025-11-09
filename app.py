import os
import textwrap
import asyncio
import logging
import json
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
    PicklePersistence,
)
from google import genai
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import PlainTextResponse

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
# 2. ИНИЦИАЛИЗАЦИЯ PTB
# =================================================================

# Создание Application
persistence = PicklePersistence(filepath="fit_ai_persistence")

# Убираем .updater(None) и делаем простой build, т.к. мы используем Starlette для Webhook
application = Application.builder().token(TELEGRAM_TOKEN).arbitrary_callback_data(True).persistence(persistence).build()
application.initialize() 


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

    get_chat_session(chat_id, context) # Пересоздаем сессию
    
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
# 5. ФУНКЦИИ STARLETTE (ASGI Web App) - АСИНХРОННЫЙ РОУТ
# =================================================================

async def start_page(request):
    """Главный роут для проверки, что Web App работает."""
    return PlainTextResponse('FIT AI Webhook ASGI is running!', 200)

async def set_webhook_route(request):
    """Установка вебхука (АСИНХРОННАЯ)."""
    # Render предоставляет имя хоста в переменной RENDER_EXTERNAL_HOSTNAME
    HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not HOSTNAME:
        return PlainTextResponse("Ошибка: Переменная RENDER_EXTERNAL_HOSTNAME не найдена.", 500)
        
    WEBHOOK_URL = f"https://{HOSTNAME}/webhook"
    
    try:
        # Устанавливаем полный URL для Webhook
        await application.bot.set_webhook(url=WEBHOOK_URL)
        return PlainTextResponse("Webhook установлен успешно!", 200)
    except Exception as e:
        logger.error(f"Ошибка при установке Webhook: {e}")
        return PlainTextResponse(f"Ошибка Telegram API: {e}", 500)

async def webhook_route(request):
    """Принимает JSON-обновление от Telegram."""
    if request.method == "POST":
        try:
            body = await request.json()
            # process_update теперь асинхронный и должен вызываться через await
            await application.process_update(
                Update.de_json(body, application.bot)
            )
            return PlainTextResponse("ok")
        except Exception as e:
            logger.error(f"Ошибка обработки Webhook: {e}")
            return PlainTextResponse("Webhook processing error", 500)
    return PlainTextResponse("Error: Method not allowed", 405)


# Создание ASGI приложения с роутами
routes = [
    Route("/", endpoint=start_page),
    Route("/set_webhook", endpoint=set_webhook_route),
    Route("/webhook", endpoint=webhook_route, methods=["POST"]),
]

# Глобальный псевдоним для Uvicorn
application_pa = Starlette(routes=routes)
