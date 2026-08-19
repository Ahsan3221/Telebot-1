import os
import logging
import asyncio

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor

from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG — sab kuch environment variables se aata hai ab.
# Railway pe / .env file mein set karo, code mein hardcode nahi karna.
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "")

SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID", "0"))

# Comma-separated Telegram user IDs, e.g. "123456789,987654321"
_staff_env = os.getenv("AUTHORIZED_STAFF_IDS", "")
ENV_AUTHORIZED_STAFF = {
    int(x.strip()) for x in _staff_env.split(",") if x.strip().isdigit()
}

# Loaded fresh at startup (env ∪ DB staff table), updated live by
# /addstaff and /removestaff — no redeploy needed to manage staff.
AUTHORIZED_STAFF: set[int] = set(ENV_AUTHORIZED_STAFF)

# ============================================================
# GAMES
# ============================================================

GAMES = [
    "Orionstars",
    "Firekirin",
    "Ultrapanda",
    "Juwa",
    "GameVault",
    "Riversweeps",
    "Milkyway",
    "Vblink",
    "Gameroom",
]

# ============================================================
# SPAM / FLOOD PROTECTION — queue + pacing (drop nahi karta)
#
# Purana approach (5 msgs / 10 sec ke baad silent drop) is liye
# hataya gaya kyunki: (1) multiple screenshots ek sath bhejne wale
# genuine customers bhi block ho jate the, (2) customer ko pata hi
# nahi chalta tha ke uska message gaya nahi — wo reply ka wait karta
# rehta, agent us message ka jo aaya hi nahi.
#
# Naya approach: har customer ka apna message-queue hai. Messages kabhi
# drop nahi hote — bas agar bohot tezi se aa rahe hon to har message ke
# beech MIN_GAP_SECONDS ka chhota sa pacing gap de diya jata hai, order
# wahi rehta hai jisme customer ne bheja. Sirf agar queue genuinely
# bohot bhar jaye (MAX_QUEUE_SIZE se zyada — matlab bot-jaisa flood,
# normal screenshot burst mein kabhi nahi hoga) tab customer ko ek
# saaf warning milti hai — silent kuch nahi hota.
# ============================================================

MIN_GAP_SECONDS = 0.4      # har forwarded message ke beech itna gap
MAX_QUEUE_SIZE = 30        # itne messages ek waqt mein pending ho sakte hain
WORKER_IDLE_TIMEOUT = 5    # itne second khali rehne pe worker band ho jata hai

_user_queues: dict[int, asyncio.Queue] = {}
_user_workers: dict[int, asyncio.Task] = {}


def _get_or_create_queue(user_id: int) -> asyncio.Queue:
    if user_id not in _user_queues:
        _user_queues[user_id] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    return _user_queues[user_id]


async def _queue_worker(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Ek customer ke queued messages ko order mein, thoda gap de ke
    forward karta hai. Queue khali ho jaye to khud band ho jata hai
    (memory mein hamesha ke liye nahi baitha rehta)."""

    queue = _user_queues[user_id]

    try:
        while True:

            try:
                user, update, topic_id = await asyncio.wait_for(
                    queue.get(), timeout=WORKER_IDLE_TIMEOUT
                )
            except asyncio.TimeoutError:
                break

            success = await _forward_customer_message(
                user=user,
                update=update,
                context=context,
                topic_id=topic_id,
            )

            if not success:
                try:
                    await update.message.reply_text(
                        "⚠️ Could not send message. Please try again."
                    )
                except Exception:
                    pass

            await asyncio.sleep(MIN_GAP_SECONDS)

    finally:
        _user_workers.pop(user_id, None)
        _user_queues.pop(user_id, None)


async def enqueue_customer_message(
    user,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    topic_id: int,
):
    queue = _get_or_create_queue(user.id)

    try:
        queue.put_nowait((user, update, topic_id))

    except asyncio.QueueFull:
        # Ye normal screenshot-burst mein practically kabhi nahi hoga —
        # sirf genuine flood/script-spam mein trigger hota hai.
        await update.message.reply_text(
            "⚠️ Bohot zyada messages ek sath aa rahe hain — "
            "thoda ruk ke bhejein, hum sab dekh lenge."
        )
        return

    existing = _user_workers.get(user.id)
    if existing is None or existing.done():
        _user_workers[user.id] = asyncio.create_task(
            _queue_worker(user.id, context)
        )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ============================================================
# DATABASE — PostgreSQL, connection pool (thread-safe) +
# async wrappers (asyncio.to_thread) so DB calls never block
# the bot's event loop.
# ============================================================

_pool: pg_pool.ThreadedConnectionPool | None = None


def _get_pool() -> pg_pool.ThreadedConnectionPool:

    global _pool

    database_url = os.getenv("DATABASE_URL", "")

    if not database_url:
        raise Exception(
            "DATABASE_URL not set! "
            "Railway pe PostgreSQL add karo."
        )

    if _pool is None:
        _pool = pg_pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=database_url,
            cursor_factory=RealDictCursor,
        )
        logger.info("PostgreSQL connection pool created.")

    return _pool


def _run(fn, *args, **kwargs):
    """
    Pool se ek connection lo, sync function run karo, commit/rollback
    handle karo, connection wapas pool mein daal do.
    Thread-safe hai — har call apna alag connection use karta hai.
    """

    pool = _get_pool()
    conn = pool.getconn()

    try:
        with conn.cursor() as cur:
            result = fn(cur, *args, **kwargs)
        conn.commit()
        return result

    except Exception:
        conn.rollback()
        raise

    finally:
        pool.putconn(conn)


def init_db():

    def _init(cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       BIGINT PRIMARY KEY,
                name          TEXT NOT NULL DEFAULT '',
                username      TEXT NOT NULL DEFAULT '',
                topic_id      BIGINT,
                topic_status  TEXT NOT NULL DEFAULT 'open',
                game          TEXT,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS topics (
                topic_id    BIGINT PRIMARY KEY,
                user_id     BIGINT NOT NULL
                            REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS staff (
                user_id     BIGINT PRIMARY KEY,
                name        TEXT NOT NULL DEFAULT '',
                added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        # Migration-safe: agar 'users' table already existed (purane
        # deploys se) to naya column bhi add ho jayega bina data toote.
        cur.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS topic_status TEXT NOT NULL DEFAULT 'open';
        """)

    _run(_init)
    logger.info("Database initialized.")


def _load_staff_sync(cur):
    cur.execute("SELECT user_id FROM staff")
    return {row["user_id"] for row in cur.fetchall()}


def load_staff_into_memory():
    """Startup pe DB staff + env staff ko merge karo."""
    db_staff = _run(_load_staff_sync)
    AUTHORIZED_STAFF.update(db_staff)
    logger.info("Authorized staff loaded: %s", AUTHORIZED_STAFF)


# ---------- async DB helpers (sab non-blocking hain) ----------

async def db_get_user(user_id: int):

    def _q(cur):
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        return cur.fetchone()

    return await asyncio.to_thread(_run, _q)


async def db_upsert_user(
    user_id: int,
    name: str,
    username: str,
    topic_id: int | None = None,
    game: str | None = None,
    topic_status: str | None = None,
):

    def _q(cur):
        cur.execute("""
            INSERT INTO users
                (user_id, name, username, topic_id, game, topic_status)
            VALUES (%s, %s, %s, %s, %s, COALESCE(%s, 'open'))
            ON CONFLICT(user_id) DO UPDATE SET
                name         = EXCLUDED.name,
                username     = EXCLUDED.username,
                topic_id     = COALESCE(EXCLUDED.topic_id, users.topic_id),
                game         = COALESCE(EXCLUDED.game, users.game),
                topic_status = COALESCE(%s, users.topic_status),
                last_seen    = NOW()
        """, (user_id, name, username, topic_id, game, topic_status, topic_status))

    await asyncio.to_thread(_run, _q)


async def db_get_user_by_topic(topic_id: int):

    def _q(cur):
        cur.execute("SELECT * FROM users WHERE topic_id = %s", (topic_id,))
        return cur.fetchone()

    return await asyncio.to_thread(_run, _q)


async def db_add_topic_mapping(topic_id: int, user_id: int):

    def _q(cur):
        cur.execute("""
            INSERT INTO topics (topic_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT(topic_id) DO UPDATE SET
                user_id = EXCLUDED.user_id
        """, (topic_id, user_id))

    await asyncio.to_thread(_run, _q)


async def db_set_topic_status(user_id: int, status: str):
    """Topic close/reopen ke liye — topic_id ko NULL nahi karta,
    isliye history/mapping intact rehti hai aur dobara reopen ho sakta hai."""

    def _q(cur):
        cur.execute(
            "UPDATE users SET topic_status = %s WHERE user_id = %s",
            (status, user_id)
        )

    await asyncio.to_thread(_run, _q)


async def db_clear_user_topic(user_id: int, topic_id: int):
    """Sirf tab use hota hai jab topic Telegram pe genuinely delete/
    missing ho gaya ho (naya topic banana zaroori hai)."""

    def _q(cur):
        cur.execute(
            "UPDATE users SET topic_id = NULL, topic_status = 'open' "
            "WHERE user_id = %s",
            (user_id,)
        )
        cur.execute("DELETE FROM topics WHERE topic_id = %s", (topic_id,))

    await asyncio.to_thread(_run, _q)


async def db_get_all_users():

    def _q(cur):
        cur.execute("SELECT * FROM users")
        return cur.fetchall()

    return await asyncio.to_thread(_run, _q)


async def db_add_staff(user_id: int, name: str):

    def _q(cur):
        cur.execute("""
            INSERT INTO staff (user_id, name)
            VALUES (%s, %s)
            ON CONFLICT(user_id) DO UPDATE SET name = EXCLUDED.name
        """, (user_id, name))

    await asyncio.to_thread(_run, _q)


async def db_remove_staff(user_id: int):

    def _q(cur):
        cur.execute("DELETE FROM staff WHERE user_id = %s", (user_id,))

    await asyncio.to_thread(_run, _q)


# ============================================================
# PER-USER LOCKS (topic creation race-condition guard)
# ============================================================

TOPIC_CREATION_LOCKS: dict[int, asyncio.Lock] = {}


def _get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in TOPIC_CREATION_LOCKS:
        TOPIC_CREATION_LOCKS[user_id] = asyncio.Lock()
    return TOPIC_CREATION_LOCKS[user_id]


def _release_lock_if_idle(user_id: int):
    """Choti si memory-leak fix — lock free hone pe dict se hata do."""
    lock = TOPIC_CREATION_LOCKS.get(user_id)
    if lock and not lock.locked():
        TOPIC_CREATION_LOCKS.pop(user_id, None)


# ============================================================
# HELPERS
# ============================================================

def games_keyboard():

    buttons = []

    for i in range(0, len(GAMES), 2):

        row = [
            InlineKeyboardButton(
                f"🎮 {GAMES[i]}",
                callback_data=f"game:{GAMES[i]}"
            )
        ]

        if i + 1 < len(GAMES):

            row.append(
                InlineKeyboardButton(
                    f"🎮 {GAMES[i + 1]}",
                    callback_data=f"game:{GAMES[i + 1]}"
                )
            )

        buttons.append(row)

    return InlineKeyboardMarkup(buttons)


def get_player_name(user) -> str:

    name = (
        user.full_name
        or user.first_name
        or "Unknown Player"
    )

    name = name.replace("\n", " ").strip()

    if user.username:
        return f"{name} (@{user.username})"

    return f"{name} [ID: {user.id}]"


def is_authorized(message) -> bool:

    if not message.from_user:
        return False

    return message.from_user.id in AUTHORIZED_STAFF


def _parse_deeplink_game(args: list[str]) -> str | None:

    if not args:
        return None

    param = args[0].lower().strip()

    for game in GAMES:

        if game.lower() == param:
            return game

    return None


# ============================================================
# CLOSED / MISSING TOPIC ERROR DETECTION
# ============================================================

def _is_closed_topic_error(error: TelegramError) -> bool:

    error_lower = str(error).lower()

    closed_keywords = [
        "topic_closed",
        "thread_not_found",
        "message thread not found",
    ]

    return any(kw in error_lower for kw in closed_keywords)


# ============================================================
# TOPIC CREATION / REOPEN
# ============================================================

async def get_or_create_topic(
    user,
    context: ContextTypes.DEFAULT_TYPE,
    pre_selected_game: str | None = None,
) -> int | None:

    user_id = user.id
    lock = _get_lock(user_id)

    async with lock:

        row = await db_get_user(user_id)

        if row and row["topic_id"]:

            if row.get("topic_status") == "closed":
                topic_id = await _reopen_or_recreate_topic(
                    user, row["topic_id"], context
                )
            else:
                topic_id = row["topic_id"]

        else:
            topic_id = await _create_new_topic(
                user,
                context,
                pre_selected_game=pre_selected_game
            )

    _release_lock_if_idle(user_id)
    return topic_id


async def _create_new_topic(
    user,
    context: ContextTypes.DEFAULT_TYPE,
    pre_selected_game: str | None = None,
) -> int | None:

    topic_name = get_player_name(user)[:120]

    try:

        topic = await context.bot.create_forum_topic(
            chat_id=SUPPORT_GROUP_ID,
            name=topic_name
        )

        topic_id = topic.message_thread_id

        existing = await db_get_user(user.id)

        existing_game = (
            existing["game"]
            if existing and existing["game"]
            else pre_selected_game
        )

        await db_upsert_user(
            user_id=user.id,
            name=user.full_name or user.first_name or "Unknown",
            username=user.username or "",
            topic_id=topic_id,
            game=existing_game,
            topic_status="open",
        )

        await db_add_topic_mapping(topic_id, user.id)

        game_line = (
            f"🎮 Game     : {existing_game}"
            if existing_game
            else "🎮 Game     : Not selected yet"
        )

        entry_line = (
            "🔗 Entry    : Deep-link"
            if pre_selected_game
            else "🔗 Entry    : Direct"
        )

        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=topic_id,
            text=(
                "👤 NEW PLAYER CONNECTED\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Name     : {user.full_name or user.first_name or 'Unknown'}\n"
                f"Username : @{user.username or 'No username'}\n"
                f"ID       : {user.id}\n"
                f"{game_line}\n"
                f"{entry_line}\n\n"
                "💬 Customer messages below.\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        )

        logger.info(
            "Created topic %s for customer %s | game: %s",
            topic_id,
            user.id,
            existing_game or "none"
        )

        return topic_id

    except TelegramError as e:

        logger.error("Topic creation failed: %s", e)

        try:

            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    "⚠️ Support system temporarily unavailable. "
                    "Please try again."
                )
            )

        except Exception:
            pass

        return None


async def _reopen_or_recreate_topic(
    user,
    topic_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> int | None:
    """
    Purane closed topic ko reopen karne ki koshish karta hai (history
    preserve rehti hai). Agar topic Telegram side se genuinely delete
    ho chuka hai to naya topic bana deta hai.
    """

    try:

        await context.bot.reopen_forum_topic(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=topic_id
        )

        await db_set_topic_status(user.id, "open")

        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=topic_id,
            text="🔓 Customer wapas aa gaya — topic reopened."
        )

        logger.info("Reopened topic %s for user %s", topic_id, user.id)

        return topic_id

    except TelegramError as e:

        logger.warning(
            "Reopen failed for topic %s (%s) — creating fresh topic",
            topic_id, e
        )

        await db_clear_user_topic(user.id, topic_id)

        return await _create_new_topic(user, context)


# ============================================================
# SEND TO GROUP — with closed/missing topic retry
# ============================================================

async def _send_to_group(
    user,
    context: ContextTypes.DEFAULT_TYPE,
    topic_id: int,
    text: str,
) -> int:

    try:

        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=topic_id,
            text=text
        )

        return topic_id

    except TelegramError as e:

        if _is_closed_topic_error(e):

            new_topic_id = await _reopen_or_recreate_topic(
                user, topic_id, context
            )

            if new_topic_id:

                try:

                    await context.bot.send_message(
                        chat_id=SUPPORT_GROUP_ID,
                        message_thread_id=new_topic_id,
                        text=text
                    )

                    return new_topic_id

                except TelegramError as e2:

                    logger.error("Send failed after reopen/recreate: %s", e2)

        else:

            logger.error("Send to group failed: %s", e)

    return topic_id


# ============================================================
# FORWARD CUSTOMER MESSAGE
# FIX: ab har message ke sath poora Name/Username/ID/Game header
# nahi jata — sirf customer ka actual message copy hota hai.
# Wo info topic ke naam + "NEW PLAYER CONNECTED" card mein already
# maujood hai, aur /id command se kabhi bhi dobara mangwaya ja sakta hai.
# ============================================================

async def _forward_customer_message(
    user,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    topic_id: int,
    _is_retry: bool = False,
) -> bool:

    try:

        await context.bot.copy_message(
            chat_id=SUPPORT_GROUP_ID,
            from_chat_id=user.id,
            message_id=update.message.message_id,
            message_thread_id=topic_id
        )

        logger.info("Customer %s → topic %s", user.id, topic_id)

        return True

    except TelegramError as e:

        if _is_closed_topic_error(e) and not _is_retry:

            new_topic_id = await _reopen_or_recreate_topic(
                user, topic_id, context
            )

            if not new_topic_id:
                return False

            return await _forward_customer_message(
                user=user,
                update=update,
                context=context,
                topic_id=new_topic_id,
                _is_retry=True,
            )

        logger.error("Message forward failed: %s", e)

        return False


# ============================================================
# START — group mein notify + deep-link game select
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    pre_selected_game = _parse_deeplink_game(
        context.args or []
    )

    topic_id = await get_or_create_topic(
        user,
        context,
        pre_selected_game=pre_selected_game
    )

    if not topic_id:
        return

    if pre_selected_game:

        await db_upsert_user(
            user_id=user.id,
            name=user.full_name or user.first_name or "Unknown",
            username=user.username or "",
            topic_id=topic_id,
            game=pre_selected_game,
        )

        await db_add_topic_mapping(topic_id, user.id)

        await _send_to_group(
            user,
            context,
            topic_id,
            text=(
                "🔗 DEEP-LINK ENTRY\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Name     : {user.full_name or user.first_name or 'Unknown'}\n"
                f"🔗 Username : @{user.username or 'No username'}\n"
                f"🆔 ID       : {user.id}\n"
                f"🎮 Game     : {pre_selected_game}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        )

        await update.message.reply_text(
            f"👋 Welcome to WishWheel Support!\n\n"
            f"🎮 Game selected: {pre_selected_game}\n\n"
            "💬 Send your message — "
            "our team will assist you right away!"
        )

    else:

        await _send_to_group(
            user,
            context,
            topic_id,
            text=(
                "🆕 CUSTOMER STARTED BOT\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Name     : {user.full_name or user.first_name or 'Unknown'}\n"
                f"🔗 Username : @{user.username or 'No username'}\n"
                f"🆔 ID       : {user.id}\n"
                f"🎮 Game     : Not selected yet\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        )

        await update.message.reply_text(
            "👋 Welcome to WishWheel Support!\n\n"
            "🎮 Select your game below (optional).\n\n"
            "💬 Or simply send your message — "
            "our team will assist you right away!",
            reply_markup=games_keyboard()
        )


# ============================================================
# GAMES COMMAND
# ============================================================

async def games(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    await get_or_create_topic(
        update.effective_user,
        context
    )

    await update.message.reply_text(
        "🎮 Choose your game:",
        reply_markup=games_keyboard()
    )


# ============================================================
# SUPPORT COMMAND
# ============================================================

async def support(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    await get_or_create_topic(
        update.effective_user,
        context
    )

    await update.message.reply_text(
        "💬 Select your game (optional):",
        reply_markup=games_keyboard()
    )


# ============================================================
# GAME SELECTION
# ============================================================

async def game_selected(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user

    game = query.data.replace("game:", "", 1)

    if game not in GAMES:

        await query.message.reply_text(
            "⚠️ Invalid game selection."
        )

        return

    topic_id = await get_or_create_topic(user, context)

    if not topic_id:
        return

    await db_upsert_user(
        user_id=user.id,
        name=user.full_name or user.first_name or "Unknown",
        username=user.username or "",
        topic_id=topic_id,
        game=game,
    )

    await db_add_topic_mapping(topic_id, user.id)

    await _send_to_group(
        user,
        context,
        topic_id,
        text=f"🎮 GAME SELECTED: {game}"
    )

    await query.message.reply_text(
        f"✅ {game} selected!\n\n"
        "💬 Send your message — team will reply shortly!"
    )


# ============================================================
# CUSTOMER MESSAGE
# ============================================================

async def customer_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    topic_id = await get_or_create_topic(user, context)

    if not topic_id:
        return

    # Drop nahi karta — queue mein daal deta hai, order + delivery
    # dono guaranteed hain. Dekho comment upar "SPAM / FLOOD PROTECTION".
    await enqueue_customer_message(
        user=user,
        update=update,
        context=context,
        topic_id=topic_id,
    )


# ============================================================
# SUPPORT GROUP MESSAGE (agent → customer)
# ============================================================

async def support_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    message = update.message

    if message.chat.id != SUPPORT_GROUP_ID:
        return

    if message.from_user and message.from_user.is_bot:
        return

    topic_id = message.message_thread_id

    if not topic_id or topic_id == 1:
        return

    row = await db_get_user_by_topic(topic_id)

    if not row:
        return

    customer_id = int(row["user_id"])

    try:

        await context.bot.copy_message(
            chat_id=customer_id,
            from_chat_id=SUPPORT_GROUP_ID,
            message_id=message.message_id
        )

        logger.info(
            "Agent reply → customer %s | topic %s",
            customer_id,
            topic_id
        )

    except TelegramError as e:

        logger.error("Reply delivery failed: %s", e)

        error_lower = str(e).lower()

        if any(kw in error_lower for kw in [
            "blocked",
            "bot was blocked",
            "user is deactivated",
            "chat not found",
            "forbidden",
        ]):

            warning_text = (
                "⚠️ DELIVERY FAILED\n\n"
                "Customer ne bot block kar diya hai "
                "ya account deactivate hai.\n"
                "Reply deliver nahi hua."
            )

        else:

            warning_text = (
                f"⚠️ Reply delivery error: {e}\n"
                "Please try again."
            )

        try:

            await context.bot.send_message(
                chat_id=SUPPORT_GROUP_ID,
                message_thread_id=topic_id,
                text=warning_text
            )

        except Exception:
            pass


# ============================================================
# /stats COMMAND
# ============================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if update.message.chat.id != SUPPORT_GROUP_ID:
        return

    if not is_authorized(update.message):

        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    def _q(cur):
        cur.execute("SELECT COUNT(*) AS c FROM users")
        total = cur.fetchone()["c"]

        cur.execute(
            "SELECT COUNT(*) AS c FROM users "
            "WHERE created_at >= NOW() - INTERVAL '1 day'"
        )
        new_today = cur.fetchone()["c"]

        cur.execute(
            "SELECT COUNT(*) AS c FROM users "
            "WHERE topic_id IS NOT NULL AND topic_status = 'open'"
        )
        active = cur.fetchone()["c"]

        cur.execute("""
            SELECT game, COUNT(*) AS c
            FROM users
            WHERE game IS NOT NULL
            GROUP BY game
            ORDER BY c DESC
        """)
        game_rows = cur.fetchall()

        return total, new_today, active, game_rows

    total, new_today, active, game_rows = await asyncio.to_thread(_run, _q)

    if game_rows:

        game_lines = "\n".join(
            f"  {row['game']:<15} {row['c']} players"
            for row in game_rows
        )

    else:

        game_lines = "  No game data yet."

    await update.message.reply_text(
        "📊 BOT STATISTICS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Customers : {total}\n"
        f"🆕 New Today       : {new_today}\n"
        f"💬 Active Topics   : {active}\n\n"
        "🎮 GAME BREAKDOWN\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{game_lines}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# /broadcast COMMAND
# Personalization: agar text mein {name} likho to har customer
# ke apne first name se replace ho jata hai. Bhejta ye individually
# hi hai (ek-ek user ko alag message) — group blast nahi hota.
#
# Usage:
#   /broadcast Hi {name}, naya game Ultrapanda add ho gaya!
# ============================================================

async def broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if update.message.chat.id != SUPPORT_GROUP_ID:
        return

    if not is_authorized(update.message):

        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    broadcast_text = " ".join(context.args or []).strip()

    if not broadcast_text:

        await update.message.reply_text(
            "⚠️ Usage: /broadcast <message>\n\n"
            "Personalize with {name} — har customer ka apna\n"
            "first name automatically lag jayega:\n\n"
            "Example:\n"
            "/broadcast Hi {name}, naya game Ultrapanda add ho gaya!"
        )

        return

    all_users = await db_get_all_users()

    if not all_users:

        await update.message.reply_text(
            "❌ No customers in database yet."
        )

        return

    progress_msg = await update.message.reply_text(
        f"📡 Broadcasting to {len(all_users)} customers..."
    )

    success_count = 0
    fail_count = 0

    for row in all_users:

        try:

            first_name = (row["name"] or "there").split(" ")[0]
            personalized = broadcast_text.replace("{name}", first_name)

            await context.bot.send_message(
                chat_id=row["user_id"],
                text=f"📢 WishWheel Update\n\n{personalized}"
            )

            success_count += 1

            await asyncio.sleep(0.05)

        except TelegramError as e:

            fail_count += 1

            logger.warning(
                "Broadcast failed for user %s: %s",
                row["user_id"],
                e
            )

    try:

        await progress_msg.edit_text(
            "📡 BROADCAST COMPLETE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Delivered : {success_count}\n"
            f"❌ Failed    : {fail_count}\n"
            f"📨 Total     : {len(all_users)}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    except Exception:
        pass


# ============================================================
# /id COMMAND
# ============================================================

async def customer_info(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    message = update.message

    if message.chat.id != SUPPORT_GROUP_ID:
        return

    if not is_authorized(message):

        await message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    topic_id = message.message_thread_id

    if not topic_id:

        await message.reply_text(
            "⚠️ Use /id inside a customer topic."
        )

        return

    row = await db_get_user_by_topic(topic_id)

    if not row:

        await message.reply_text(
            "❌ No customer connected to this topic."
        )

        return

    username = row["username"] or ""
    game = row["game"] or "Not selected"

    await message.reply_text(
        "📋 CUSTOMER INFORMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name     : {row['name']}\n"
        f"🔗 Username : @{username or 'No username'}\n"
        f"🆔 ID       : {row['user_id']}\n"
        f"🎮 Game     : {game}\n"
        f"📌 Topic ID : {topic_id}\n"
        f"📅 Joined   : {row['created_at']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# /close COMMAND
# FIX: ab topic_id NULL nahi hota — status sirf 'closed' hota hai.
# Customer dobara message kare to wahi purana topic reopen hota hai,
# history preserve rehti hai, group mein orphan topics nahi bante.
# ============================================================

async def close_topic(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    message = update.message

    if message.chat.id != SUPPORT_GROUP_ID:
        return

    if not is_authorized(message):

        await message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    topic_id = message.message_thread_id

    if not topic_id or topic_id == 1:

        await message.reply_text(
            "⚠️ Use /close inside a customer topic."
        )

        return

    row = await db_get_user_by_topic(topic_id)

    if not row:

        await message.reply_text(
            "❌ No customer connected to this topic."
        )

        return

    customer_id = int(row["user_id"])

    try:

        try:

            await context.bot.send_message(
                chat_id=customer_id,
                text=(
                    "✅ Support conversation closed.\n\n"
                    "Need help again? Just send a message!"
                )
            )

        except TelegramError:
            pass

        await context.bot.close_forum_topic(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=topic_id
        )

        await db_set_topic_status(customer_id, "closed")

        await message.reply_text(
            "✅ Topic closed.\n"
            "Customer dobara message kare to yehi topic reopen hoga."
        )

        logger.info(
            "Topic %s closed | customer %s",
            topic_id,
            customer_id
        )

    except TelegramError as e:

        logger.error("Topic close failed: %s", e)

        await message.reply_text(
            f"⚠️ Error closing topic: {e}"
        )


# ============================================================
# /addstaff, /removestaff, /staff COMMANDS
# Naya support agent add karne ke liye redeploy ki zarurat nahi.
# Customer ki ID /id command se topic ke andar mil jati hai.
# ============================================================

async def add_staff(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if update.message.chat.id != SUPPORT_GROUP_ID:
        return

    if not is_authorized(update.message):

        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    args = context.args or []

    if not args or not args[0].lstrip("-").isdigit():

        await update.message.reply_text(
            "⚠️ Usage: /addstaff <telegram_id> [name]\n\n"
            "Tip: customer ki ID unke /id output se mil jati hai — "
            "naye staff member ko apna Telegram ID bhejne ko kaho, "
            "ya @userinfobot use karwao."
        )

        return

    new_id = int(args[0])
    name = " ".join(args[1:]) or "Staff"

    await db_add_staff(new_id, name)
    AUTHORIZED_STAFF.add(new_id)

    await update.message.reply_text(
        f"✅ {name} (ID: {new_id}) ab authorized staff hai."
    )


async def remove_staff(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if update.message.chat.id != SUPPORT_GROUP_ID:
        return

    if not is_authorized(update.message):

        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    args = context.args or []

    if not args or not args[0].lstrip("-").isdigit():

        await update.message.reply_text(
            "⚠️ Usage: /removestaff <telegram_id>"
        )

        return

    remove_id = int(args[0])

    await db_remove_staff(remove_id)
    AUTHORIZED_STAFF.discard(remove_id)

    await update.message.reply_text(
        f"✅ ID {remove_id} ko staff se remove kar diya."
    )


async def list_staff(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if update.message.chat.id != SUPPORT_GROUP_ID:
        return

    if not is_authorized(update.message):

        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    ids_text = "\n".join(f"  {uid}" for uid in sorted(AUTHORIZED_STAFF)) or "  (none)"

    await update.message.reply_text(
        "👥 AUTHORIZED STAFF\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{ids_text}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.error(
        "Unhandled error: %s",
        context.error,
        exc_info=True
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 42)
    print("       WISHWHEEL SUPPORT BOT")
    print("=" * 42)

    if not TOKEN:
        print("❌ BOT_TOKEN not set!")
        return

    if not SUPPORT_GROUP_ID:
        print("❌ SUPPORT_GROUP_ID not set! (.env mein daalo)")
        return

    init_db()
    load_staff_into_memory()

    print(f"Support Group   : {SUPPORT_GROUP_ID}")
    print(f"Authorized Staff: {AUTHORIZED_STAFF}")
    print("=" * 42)

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("games", games))
    application.add_handler(CommandHandler("support", support))

    application.add_handler(
        CallbackQueryHandler(game_selected, pattern=r"^game:")
    )

    application.add_handler(
        CommandHandler(
            "id", customer_info,
            filters=filters.Chat(chat_id=SUPPORT_GROUP_ID)
        )
    )

    application.add_handler(
        CommandHandler(
            "close", close_topic,
            filters=filters.Chat(chat_id=SUPPORT_GROUP_ID)
        )
    )

    application.add_handler(
        CommandHandler(
            "stats", stats,
            filters=filters.Chat(chat_id=SUPPORT_GROUP_ID)
        )
    )

    application.add_handler(
        CommandHandler(
            "broadcast", broadcast,
            filters=filters.Chat(chat_id=SUPPORT_GROUP_ID)
        )
    )

    application.add_handler(
        CommandHandler(
            "addstaff", add_staff,
            filters=filters.Chat(chat_id=SUPPORT_GROUP_ID)
        )
    )

    application.add_handler(
        CommandHandler(
            "removestaff", remove_staff,
            filters=filters.Chat(chat_id=SUPPORT_GROUP_ID)
        )
    )

    application.add_handler(
        CommandHandler(
            "staff", list_staff,
            filters=filters.Chat(chat_id=SUPPORT_GROUP_ID)
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            customer_message
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Chat(chat_id=SUPPORT_GROUP_ID) & ~filters.COMMAND,
            support_group_message
        )
    )

    application.add_error_handler(error_handler)

    print("Bot is running...")
    print("=" * 42)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
