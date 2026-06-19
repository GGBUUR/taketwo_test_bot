import yfinance as yf
import json
import logging
import os
import time
import uuid

from telegram import (
    Update,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

# =====================
# LOGGING
# =====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("multi_stock_bot")


# =====================
# SETTINGS
# =====================
# Токен лучше не хранить в коде — используйте переменную окружения:
#   export STOCKBOT_TOKEN="ваш_токен"
TOKEN = os.environ.get("STOCKBOT_TOKEN", "TOKEN_NOT_SET")

THRESHOLD = 1.5

# Список-фоллбэк на случай, если не получится подтянуть тикеры из интернета
FALLBACK_TICKERS = ["TTWO", "AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "GOOGL", "META"]

# Какой скринер использовать для динамического списка:
# "most_actives" — самые торгуемые по объёму, "day_gainers" — лидеры роста,
# "day_losers" — лидеры падения
TRENDING_SCREEN = "most_actives"
TRENDING_COUNT = 10
TRENDING_TTL = 3600  # сек — как долго кэшировать список, чтобы не дёргать API лишний раз

_trending_cache = {"tickers": [], "time": 0}


def get_suggested_tickers() -> list:
    """Список тикеров для кнопок выбора. Берётся из интернета (скринер yfinance),
    с кэшем на TRENDING_TTL секунд и фоллбэком на FALLBACK_TICKERS при ошибке."""
    now = time.time()

    if _trending_cache["tickers"] and (now - _trending_cache["time"] < TRENDING_TTL):
        return _trending_cache["tickers"]

    try:
        result = yf.screen(TRENDING_SCREEN, count=TRENDING_COUNT)
        quotes = result.get("quotes", [])
        tickers = [q["symbol"] for q in quotes if q.get("symbol")]

        if tickers:
            _trending_cache["tickers"] = tickers
            _trending_cache["time"] = now
            return tickers
    except Exception as e:
        logger.warning("Не удалось получить список тикеров из интернета: %s", e)

    # Не обновляем время кэша, чтобы попробовать снова при следующем запросе
    return FALLBACK_TICKERS

USER_FILE = "stockUserData.json"
STATE_FILE = "stockState.json"
HEARTBEAT_FILE = "stockHeartbeat.json"
STATUS_FILE = "stockPriceStatus.json"

# Последняя известная цена по каждому тикеру (для % изменения и алертов)
last_prices = {}


# =====================
# STORAGE
# =====================
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        return json.load(open(path, "r", encoding="utf-8"))
    except Exception as e:
        logger.error("Не удалось прочитать %s: %s", path, e)
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


user_data = load_json(USER_FILE, {})
# Структура user_data[user_id]:
# {
#   "stocks": {
#       "TTWO": {"stage": "active", "has_stock": True, "entry": 150.0, "paused": False},
#       "AAPL": {...},
#   },
#   "stage": None | "waiting_symbol" | "waiting_price" | "selecting_custom",
#   "pending_ticker": None | "AAPL",
#   "selected": [],   # тикеры, отмеченные галочкой в меню выбора (ещё не зарегистрированы)
#   "queue": [],      # тикеры, ожидающие вопроса "есть акции?" по очереди
# }


def get_user(user_id: str) -> dict:
    if user_id not in user_data:
        user_data[user_id] = {
            "stocks": {},
            "stage": None,
            "pending_ticker": None,
            "selected": [],
            "queue": [],
        }
    data = user_data[user_id]
    data.setdefault("stocks", {})
    data.setdefault("selected", [])
    data.setdefault("queue", [])
    return data


# =====================
# PRICE
# =====================
def get_price(ticker: str):
    stock = yf.Ticker(ticker)
    data = stock.history(period="1d", interval="1m")
    if data.empty:
        return None
    return float(data["Close"].iloc[-1])


def all_tracked_tickers() -> set:
    tickers = set()
    for data in user_data.values():
        tickers.update(data.get("stocks", {}).keys())
    return tickers


# =====================
# REVIZOR
# =====================
def heartbeat():
    with open(HEARTBEAT_FILE, "w") as f:
        json.dump({"time": time.time()}, f)

      
def save_status():
    with open(STATUS_FILE, "w") as f:
        json.dump(
            {
                "time": time.time(),
                "tickers": last_prices,
            },
            f,
        )


# =====================
# STATE (глобальный вкл/выкл мониторинга, как и раньше)
# =====================
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"running": True}
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"running": True}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# =====================
# МЕНЮ
# =====================
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить акцию", callback_data="add_new")],
        [InlineKeyboardButton("➖ Убрать акцию", callback_data="remove_menu")],
    ])


def yes_no_keyboard(ticker: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 Есть акции", callback_data=f"stock_yes_{ticker}"),
            InlineKeyboardButton("📉 Нет акций", callback_data=f"stock_no_{ticker}"),
        ]
    ])


def selection_keyboard(data: dict):
    selected = set(data.get("selected", []))
    already = set(data.get("stocks", {}).keys())
    suggested = get_suggested_tickers()

    rows = []
    row = []
    for t in suggested:
        if t in already:
            label = f"✔️ {t} (уже есть)"
        elif t in selected:
            label = f"☑️ {t}"
        else:
            label = f"⬜ {t}"
        row.append(InlineKeyboardButton(label, callback_data=f"toggle_{t}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("✏️ Свой тикер", callback_data="selcustom")])
    rows.append([InlineKeyboardButton("🔄 Обновить список", callback_data="selrefresh")])
    rows.append([InlineKeyboardButton(f"✅ Готово ({len(selected)})", callback_data="seldone")])
    return InlineKeyboardMarkup(rows)


async def show_selection_menu(message, data: dict):
    await message.reply_text(
        "🔥 Сейчас популярны (по объёму торгов). Отметьте акции и нажмите «Готово»:",
        reply_markup=selection_keyboard(data),
    )


async def advance_queue(message, user_id: str, data: dict):
    """Берёт следующий тикер из очереди и задаёт ему тот же вопрос «есть акции?».
    Если очередь пуста — сообщает, что всё добавлено."""
    queue = data.get("queue", [])

    if not queue:
        await message.reply_text("✅ Все выбранные акции добавлены!", reply_markup=main_menu_keyboard())
        return

    ticker = queue.pop(0)
    save_json(USER_FILE, user_data)

    await message.reply_text(
        f"👋 У вас есть акции {ticker}?",
        reply_markup=yes_no_keyboard(ticker),
    )


# =====================
# START
# =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    data = get_user(user_id)

    save_state({"running": True})

    stocks = data["stocks"]
    if stocks:
        names = ", ".join(stocks.keys())
        await update.message.reply_text(
            f"📊 Сейчас отслеживаются: {names}\n"
        )
    else:
        await update.message.reply_text("👋 Добро пожаловать! Давайте настроим, какие акции отслеживать.")
        await show_selection_menu(update.message, data)


# =====================
# STOP
# =====================
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_state(
        {
            "monitoring": False,
            "service": "alive"
        })
    await update.message.reply_text("👋 Мониторинг остановлен")


# =====================
# ДОБАВЛЕНИЕ НОВОГО ТИКЕРА — точка входа
# =====================
async def add_stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    data = get_user(user_id)
    await show_selection_menu(update.message, data)


# =====================
# УДАЛЕНИЕ ТИКЕРА — точка входа (команда /removeStock)
# =====================
async def remove_stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    stocks = get_user(user_id).get("stocks", {})

    if not stocks:
        await update.message.reply_text("📭 У вас пока нет отслеживаемых акций.")
        return

    rows = [
        [InlineKeyboardButton(f"❌ {t}", callback_data=f"remove_{t}")]
        for t in stocks.keys()
    ]
    await update.message.reply_text("Какую акцию убрать?", reply_markup=InlineKeyboardMarkup(rows))


# =====================
# CALLBACK
# =====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.message.chat.id)
    data = get_user(user_id)

    if query.data == "add_new":
        await show_selection_menu(query.message, data)
        return

    if query.data.startswith("toggle_"):
        ticker = query.data.split("_", 1)[1]
        selected = data.setdefault("selected", [])
        if ticker in selected:
            selected.remove(ticker)
        else:
            selected.append(ticker)
        save_json(USER_FILE, user_data)
        await query.message.edit_reply_markup(reply_markup=selection_keyboard(data))
        return

    if query.data == "selrefresh":
        _trending_cache["time"] = 0  # сбрасываем кэш, следующий вызов get_suggested_tickers() пойдёт в интернет
        await query.message.edit_reply_markup(reply_markup=selection_keyboard(data))
        return

    if query.data == "selcustom":
        data["stage"] = "selecting_custom"
        save_json(USER_FILE, user_data)
        await query.message.reply_text("Введите тикер акции (например AAPL, NVDA, TTWO):")
        return

    if query.data == "seldone":
        selected = [t for t in data.get("selected", []) if t not in data.get("stocks", {})]
        data["selected"] = []

        if not selected:
            await query.message.reply_text("Вы ничего не отметили. Выберите хотя бы одну акцию.")
            return

        data["queue"] = selected
        save_json(USER_FILE, user_data)
        await advance_queue(query.message, user_id, data)
        return

    if query.data == "remove_menu":
        stocks = data.get("stocks", {})
        if not stocks:
            await query.message.reply_text("📭 У вас пока нет отслеживаемых акций.")
            return
        rows = [
            [InlineKeyboardButton(f"❌ {t}", callback_data=f"remove_{t}")]
            for t in stocks.keys()
        ]
        await query.message.reply_text("Какую акцию убрать?", reply_markup=InlineKeyboardMarkup(rows))
        return

    if query.data.startswith("remove_"):
        ticker = query.data.split("_", 1)[1]
        data.get("stocks", {}).pop(ticker, None)
        save_json(USER_FILE, user_data)
        await query.message.reply_text(f"❌ {ticker} больше не отслеживается.")
        return

    # ===== Это и есть та самая исходная логика, просто с тикером в callback_data =====
    if query.data.startswith("stock_no_"):
        ticker = query.data.split("_", 2)[2]
        price = get_price(ticker)

        data["stocks"][ticker] = {
            "stage": "active",
            "has_stock": False,
            "entry": price,
            "paused": False,
        }

        save_json(USER_FILE, user_data)

        await query.message.reply_text(
            f"📉 Мониторинг {ticker} начат\nСтартовая цена: {price}"
        )

        await advance_queue(query.message, user_id, data)
        return

    if query.data.startswith("stock_yes_"):
        ticker = query.data.split("_", 2)[2]

        data["stocks"][ticker] = {
            "stage": "waiting_price",
            "has_stock": True,
            "paused": False,
        }
        data["stage"] = "waiting_price"
        data["pending_ticker"] = ticker

        save_json(USER_FILE, user_data)

        await query.message.reply_text(f"💰 Введите цену покупки {ticker}:")
        return


# =====================
# TEXT INPUT
# =====================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    data = get_user(user_id)
    stage = data.get("stage")

    # Ручной ввод тикера во время выбора (кнопка "✏️ Свой тикер")
    if stage == "selecting_custom":
        ticker = update.message.text.strip().upper()

        price = get_price(ticker)
        if price is None:
            await update.message.reply_text(f"❌ Не нашёл тикер «{ticker}». Попробуйте снова.")
            return

        if ticker not in data.get("selected", []) and ticker not in data.get("stocks", {}):
            data.setdefault("selected", []).append(ticker)

        data["stage"] = None
        save_json(USER_FILE, user_data)

        await update.message.reply_text(f"✅ {ticker} добавлен в список выбора.")
        await show_selection_menu(update.message, data)
        return

    # Шаг 1: ждём тикер
    if stage == "waiting_symbol":
        ticker = update.message.text.strip().upper()

        price = get_price(ticker)
        if price is None:
            await update.message.reply_text(f"❌ Не нашёл тикер «{ticker}». Проверьте символ и попробуйте снова.")
            return

        data["stage"] = None
        save_json(USER_FILE, user_data)

        await update.message.reply_text(
            f"👋 У вас есть акции {ticker}?",
            reply_markup=yes_no_keyboard(ticker),
        )
        return

    # Шаг 2: ждём цену покупки (та самая исходная логика, просто привязана к тикеру)
    if stage == "waiting_price":
        ticker = data.get("pending_ticker")
        try:
            entry_price = float(update.message.text)

            data["stocks"][ticker] = {
                "stage": "active",
                "has_stock": True,
                "entry": entry_price,
                "paused": False,
            }
            data["stage"] = None
            data["pending_ticker"] = None

            save_json(USER_FILE, user_data)

            await update.message.reply_text(f"✅ Цена сохранена: {ticker} — {entry_price}")

            await advance_queue(update.message, user_id, data)

        except ValueError:
            await update.message.reply_text("❌ Введите число")
        return


# =====================
# CHECK STATUS — теперь по всем отслеживаемым акциям
# =====================
# =====================
# CHECK STATUS — теперь по всем отслеживаемым акциям
# =====================
async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    stocks = get_user(user_id).get("stocks", {})

    if not stocks:
        await update.message.reply_text("📭 Нет отслеживаемых акций. Используйте «➕ Добавить акцию».")
        return

    for ticker, stock in stocks.items():
        price = get_price(ticker)

        if price is None:
            continue

        entry = stock.get("entry")

        # 💰 PnL от цены покупки
        if entry:
            pnl = ((price - entry) / entry) * 100
        else:
            pnl = 0

        # иконка
        if pnl > 0:
            icon = "📈 PROFIT"
        elif pnl < 0:
            icon = "📉 LOSS"
        else:
            icon = "➖ BREAK EVEN"

        msg = (
            f"📊 {ticker} STATUS\n"
            f"Price: {price}\n"
            f"💰 Bought at: {entry}\n"
            f"{icon} PnL: {pnl:+.2f}%"
        )

        await update.message.reply_text(msg)

# =====================
# INLINE — поиск по мировым рынкам
# =====================
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.inline_query.query.strip()

    if not query_text:
        await update.inline_query.answer(results=[], cache_time=1)
        return

    results = []

    # Пытаемся найти совпадения через поиск yfinance (название компании, тикер)
    try:
        search = yf.Search(query_text, max_results=6)
        quotes = search.quotes or []
    except Exception as e:
        logger.warning("yf.Search недоступен или ошибся: %s", e)
        quotes = []

    candidates = []
    for q in quotes:
        symbol = q.get("symbol")
        name = q.get("shortname") or q.get("longname") or symbol
        if symbol:
            candidates.append((symbol, name))

    # Фоллбэк: если поиск ничего не вернул — пробуем считать введённый текст тикером напрямую
    if not candidates:
        candidates.append((query_text.upper(), query_text.upper()))

    for symbol, name in candidates[:6]:
        price = get_price(symbol)
        if price is None:
            continue

        text = f"📊 {name} ({symbol})\nPrice: {price}"

        results.append(
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title=f"{name} ({symbol})",
                description=f"Price: {price}",
                input_message_content=InputTextMessageContent(text),
            )
        )

    await update.inline_query.answer(results=results, cache_time=1)


# =====================
# PRICE LOOP
# =====================
async def price_job(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not state.get("running", True):
        return

    tickers = all_tracked_tickers()
    if not tickers:
        heartbeat()
        save_status()
        return

    try:
        for ticker in tickers:
            price = get_price(ticker)
            if price is None:
                continue

            prev = last_prices.get(ticker)

            if prev is None:
                change = 0
            else:
                change = ((price - prev) / prev) * 100

            print(f"{ticker} | {price} | {change:+.3f}%")

            # ===== Та же логика рассылки, что и в исходнике, просто по каждому тикеру =====
            for user_id, data in user_data.items():
                stock = data.get("stocks", {}).get(ticker)

                if not stock:
                    continue

                if stock.get("paused"):
                    continue

                if stock.get("stage") == "waiting_price":
                    continue

                entry = stock.get("entry")

                pnl_text = ""

                if entry:
                    pnl = ((price - entry) / entry) * 100

                    if pnl > 0:
                        icon = "📈 STONKS"
                    elif pnl < 0:
                        icon = "📉 STONKS"
                    else:
                        icon = "➖"

                    pnl_text = f"\n💰 {icon} PnL: {pnl:+.2f}%"

                msg = (
                    f"📊 {ticker} STATUS\n"
                    f"Price: {price}\n"
                    f"{pnl_text}"
                )

                await context.application.bot.send_message(
                    chat_id=user_id,
                    text=msg
                )

            # ===== Резкое движение — уведомляем всех, кто следит именно за этим тикером =====
            if prev is not None:
                holders = [
                    uid for uid, data in user_data.items()
                    if ticker in data.get("stocks", {})
                ]

                if change >= THRESHOLD:
                    for uid in holders:
                        await context.application.bot.send_message(
                            chat_id=uid,
                            text=f"🚀 РЕЗКИЙ РОСТ {ticker}\nPrice: {price}"
                        )

                elif change <= -THRESHOLD:
                    for uid in holders:
                        await context.application.bot.send_message(
                            chat_id=uid,
                            text=f"📉 РЕЗКОЕ ПАДЕНИЕ {ticker}\nPrice: {price}"
                        )

            last_prices[ticker] = price

        heartbeat()
        save_status()

    except Exception as e:
        print("[ERROR]", e)


# =====================
# ERROR HANDLER
# =====================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Исключение при обработке апдейта:", exc_info=context.error)

def heartbeat_job(context):
    heartbeat()
# =====================
# BOT SETUP
# =====================
def main():

    app = Application.builder().token(TOKEN).build()

    if app.job_queue is None:
        raise RuntimeError('Установите: pip install "python-telegram-bot[job-queue]"')

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("addStock", add_stock_command))
    app.add_handler(CommandHandler("removeStock", remove_stock_command))
    app.add_handler(CommandHandler("checkStatus", check_status))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(heartbeat_job, interval=4, first=1)
    app.job_queue.run_repeating(price_job, interval=1800, first=5)

    logger.info("📊 Multi-stock bot started...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()