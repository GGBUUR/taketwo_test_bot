import json
import logging
import os
import time

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =====================
# LOGGING
# =====================
# Без этого python-telegram-bot почти ничего не печатает в консоль,
# и бот будет выглядеть "тихо висящим", даже если на самом деле работает
# (или, наоборот, реально не получает апдейты).
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("revisor_bot")


# =====================
# SETTINGS
# =====================
# ВАЖНО: токен лучше не хранить в коде. Используйте переменную окружения:
#   export REVISOR_BOT_TOKEN="ваш_токен"
# Если переменная не задана — используется значение ниже как fallback,
# но раз этот токен уже был показан в переписке — обязательно перевыпустите
# его через @BotFather (/revoke) и подставьте новый.
TOKEN = os.environ.get("REVISOR_BOT_TOKEN")

REGISTRY_FILE = "botsRegistry.json"

OWNER_ID = 7717520610  # ВСТАВЬ СВОЙ TELEGRAM USER ID

# Как часто проверять состояние ботов из реестра (в секундах)
MONITOR_INTERVAL = 20

# Запоминаем последнее известное состояние каждого бота,
# чтобы слать уведомление только при ИЗМЕНЕНИИ статуса, а не на каждой проверке
last_known_state = {}


# =====================
# LOAD JSON SAFE
# =====================
def load_json(path, default=None):
    if default is None:
        default = {}

    if not path:
        return default

    if not os.path.exists(path):
        logger.warning("Файл не найден: %s", path)
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Не удалось прочитать %s: %s", path, e)
        return default


# =====================
# AUTH
# =====================
def is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    authorized = user.id == OWNER_ID
    if not authorized:
        logger.info("Доступ запрещён для user_id=%s (ожидался %s)", user.id, OWNER_ID)
    return authorized


# =====================
# LOAD REGISTRY
# =====================
def load_registry():
    registry = load_json(REGISTRY_FILE, {})
    if not registry:
        logger.warning("Реестр ботов пуст или не найден: %s", REGISTRY_FILE)
    return registry


# =====================
# READ BOT DATA
# =====================
def read_bot_data(bot: dict):
    status = load_json(bot.get("status_file"), {})
    heartbeat = load_json(bot.get("heartbeat_file"), {})
    state_file = load_json(bot.get("state_file"), {})

    now = time.time()

    price = status.get("price")
    change = status.get("change")
    last_beat = heartbeat.get("time")

    monitoring = state_file.get("monitoring", True)

    # 💡 ВАЖНО: это НЕ DEAD
    if not monitoring:
        return "⏸ PAUSED (USER STOPPED)", price, change, None

    if not last_beat:
        return "❌ NO DATA", price, change, None

    lag = now - last_beat

    if lag < 7:
        state = "🟢 RUNNING"
    elif lag < 15:
        state = "🟡 LAGGING"
    else:
        state = "🔴 DEAD"

    return state, price, change, lag
# =====================
# /checkBot MENU
# =====================
async def checkbot_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("/checkBot от user_id=%s", update.effective_user.id if update.effective_user else None)

    if not is_authorized(update):
        await update.message.reply_text("⛔ Access denied")
        return

    registry = load_registry()

    if not registry:
        await update.message.reply_text(
            "❌ botsRegistry.json пуст или не найден.\n"
            f"Ожидаемый путь: {os.path.abspath(REGISTRY_FILE)}"
        )
        return

    keyboard = []

    for bot_id, bot in registry.items():
        keyboard.append([
            InlineKeyboardButton(
                f"📊 {bot.get('name', bot_id)}",
                callback_data=bot_id
            )
        ])

    await update.message.reply_text(
        "👮 Выберите бота для проверки:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =====================
# CALLBACK HANDLER
# =====================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.info("Callback от user_id=%s, data=%s", update.effective_user.id if update.effective_user else None, query.data)

    if not is_authorized(update):
        await query.answer("⛔ Access denied", show_alert=True)
        return

    await query.answer()

    bot_id = query.data
    registry = load_registry()

    bot = registry.get(bot_id)

    if not bot:
        await query.message.reply_text("❌ Bot not found")
        return

    state, price, change, lag = read_bot_data(bot)

    msg = (
        "👮 BOT STATUS REPORT\n\n"
        f"🤖 Name: {bot.get('name')}\n"
        f"🆔 ID: {bot_id}\n\n"
        f"📡 State: {state}\n"
    )

    if price is not None:
        msg += f"💰 Price: {price}\n"

    if change is not None:
        msg += f"📊 Change: {change:+.2f}%\n"

    if lag is not None:
        msg += f"⏱ Lag: {lag:.1f}s\n"

    await query.message.reply_text(msg)


# =====================
# /myid — диагностическая команда без авторизации
# =====================
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("/myid от user_id=%s", update.effective_user.id if update.effective_user else None)
    await update.message.reply_text(f"Your ID: {update.effective_user.id}")


# =====================
# АВТО-МОНИТОРИНГ: уведомление при падении бота
# =====================
async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    registry = load_registry()

    if not registry:
        return

    for bot_id, bot in registry.items():
        state, price, change, lag = read_bot_data(bot)
        prev_state = last_known_state.get(bot_id)

        # Видно на каждом цикле, какой статус бот реально видит —
        # если тут не "🔴 DEAD", когда бот объективно упал, проблема в путях
        # к status_file/heartbeat_file в botsRegistry.json
        logger.info("Проверка %s: state=%s prev=%s lag=%s", bot_id, state, prev_state, lag)

        # Уведомляем только при ПЕРЕХОДЕ в DEAD (а не на каждой проверке)
        if state == "🔴 DEAD" and prev_state != "🔴 DEAD":
            name = bot.get("name", bot_id)
            lag_text = f"{lag:.0f} сек" if lag is not None else "неизвестно"
            try:
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=(
                        f"🚨 ВНИМАНИЕ\n\n"
                        f"Бот «{name}» ({bot_id}) перестал отвечать!\n"
                        f"⏱ Последний heartbeat: {lag_text} назад\n"
                        f"📡 Статус: {state}"
                    ),
                )
                logger.warning("Бот %s (%s) ушёл в DEAD — уведомление отправлено", name, bot_id)
            except Exception as e:
                logger.error("Не удалось отправить уведомление о падении %s: %s", bot_id, e)

        # Уведомляем, когда бот ожил после падения
        elif state == "🟢 RUNNING" and prev_state == "🔴 DEAD":
            name = bot.get("name", bot_id)
            try:
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=f"✅ Бот «{name}» ({bot_id}) снова в сети",
                )
                logger.info("Бот %s (%s) восстановился — уведомление отправлено", name, bot_id)
            except Exception as e:
                logger.error("Не удалось отправить уведомление о восстановлении %s: %s", bot_id, e)

        last_known_state[bot_id] = state


# =====================
# ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК
# =====================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Исключение при обработке апдейта:", exc_info=context.error)


# =====================
# APP
# =====================
def main():

    app = Application.builder().token(TOKEN).build()

    if app.job_queue is None:
        raise RuntimeError(
            "JobQueue недоступен. Установите расширение:\n"
            "    pip install \"python-telegram-bot[job-queue]\"\n"
            "Без него фоновая проверка (monitor_job) не запустится вообще."
        )

    app.add_handler(CommandHandler("checkBot", checkbot_menu))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(monitor_job, interval=MONITOR_INTERVAL, first=10)

    logger.info("👮 Revisor bot started...")
    logger.info("Путь к реестру: %s", os.path.abspath(REGISTRY_FILE))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()