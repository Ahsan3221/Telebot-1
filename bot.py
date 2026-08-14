import os
import logging
import asyncio

import psycopg2
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
# CONFIG
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "")

SUPPORT_GROUP_ID = -1004332564341

AUTHORIZED_STAFF = {
    123456789,
    987654321,
}

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
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ============================================================
# DATABASE — PostgreSQL
# Ephemeral filesystem problem solve:
# SQLite se PostgreSQL migrate kiya
# Data redeploy ke baad bhi safe rahega
# ============================================================

_conn = None


def get_db():

    global _conn

    database_url = os.getenv("DATABASE_URL", "")

    if not database_url:
        raise Exception(
            "DATABASE_URL not set! "
            "Railway pe PostgreSQL add karo."
        )

    try:

        if _conn is None or _conn.closed:

            _conn = psycopg2.connect(
                database_url,
                cursor_factory=RealDictCursor,
                connect_timeout=10,
            )

            _conn.autocommit = False

            logger.info("PostgreSQL connected.")

    except psycopg2.OperationalError as e:

        logger.error("DB connection failed: %s", e)

        _conn = None

        raise

    return _conn


def init_db():

    conn = get_db()

    with conn.cursor() as cur:

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     BIGINT PRIMARY KEY,
                name        TEXT NOT NULL DEFAULT '',
                username    TEXT NOT NULL DEFAULT '',
                topic_id    BIGINT,
                game        TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS topics (
                topic_id    BIGINT PRIMARY KEY,
                user_id     BIGINT NOT NULL
                            REFERENCES users(user_id)
            );
        """)

        conn.commit()

    logger.info("Database initialized.")


# ============================================================
# DB HELPERS
# ============================================================

def db_get_user(user_id: int):

    conn = get_db()

    with conn.cursor() as cur:

        cur.execute(
            "SELECT * FROM users WHERE user_id = %s",
            (user_id,)
        )

        return cur.fetchone()


def db_upsert_user(
    user_id: int,
    name: str,
    username: str,
    topic_id: int | None = None,
    game: str | None = None,
):

    conn = get_db()

    with conn.cursor() as cur:

        cur.execute("""
            INSERT INTO users
                (user_id, name, username, topic_id, game)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                name      = EXCLUDED.name,
                username  = EXCLUDED.username,
                topic_id  = COALESCE(
                                EXCLUDED.topic_id,
                                users.topic_id
                            ),
                game      = COALESCE(
                                EXCLUDED.game,
                                users.game
                            ),
                last_seen = NOW()
        """, (user_id, name, username, topic_id, game))

        conn.commit()


def db_get_user_by_topic(topic_id: int):

    conn = get_db()

    with conn.cursor() as cur:

        cur.execute(
            "SELECT * FROM users WHERE topic_id = %s",
            (topic_id,)
        )

        return cur.fetchone()


def db_add_topic_mapping(topic_id: int, user_id: int):

    conn = get_db()

    with conn.cursor() as cur:

        cur.execute("""
            INSERT INTO topics (topic_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT(topic_id) DO UPDATE SET
                user_id = EXCLUDED.user_id
        """, (topic_id, user_id))

        conn.commit()


def db_clear_user_topic(user_id: int, topic_id: int):

    conn = get_db()

    with conn.cursor() as cur:

        cur.execute(
            "UPDATE users SET topic_id = NULL "
            "WHERE user_id = %s",
            (user_id,)
        )

        cur.execute(
            "DELETE FROM topics WHERE topic_id = %s",
            (topic_id,)
        )

        conn.commit()


def db_get_all_users():

    conn = get_db()

    with conn.cursor() as cur:

        cur.execute("SELECT * FROM users")

        return cur.fetchall()


# ============================================================
# PER-USER LOCKS — race condition fix
# ============================================================

TOPIC_CREATION_LOCKS: dict[int, asyncio.Lock] = {}


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
# CLOSED TOPIC ERROR DETECTION
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
# TOPIC CREATION
# ============================================================

async def get_or_create_topic(
    user,
    context: ContextTypes.DEFAULT_TYPE,
    pre_selected_game: str | None = None,
) -> int | None:

    user_id = user.id

    if user_id not in TOPIC_CREATION_LOCKS:
        TOPIC_CREATION_LOCKS[user_id] = asyncio.Lock()

    async with TOPIC_CREATION_LOCKS[user_id]:

        row = db_get_user(user_id)

        if row and row["topic_id"]:
            return row["topic_id"]

        return await _create_new_topic(
            user,
            context,
            pre_selected_game=pre_selected_game
        )


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

        existing = db_get_user(user.id)

        existing_game = (
            existing["game"]
            if existing and existing["game"]
            else pre_selected_game
        )

        db_upsert_user(
            user_id=user.id,
            name=user.full_name or user.first_name or "Unknown",
            username=user.username or "",
            topic_id=topic_id,
            game=existing_game,
        )

        db_add_topic_mapping(topic_id, user.id)

        game_line = (
            f"🎮 Game     : {existing_game} (auto-selected)"
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


async def _clear_topic_and_recreate(
    user,
    old_topic_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> int | None:

    logger.info(
        "Clearing closed topic %s for user %s",
        old_topic_id,
        user.id
    )

    db_clear_user_topic(user.id, old_topic_id)

    return await get_or_create_topic(user, context)


# ============================================================
# FORWARD CUSTOMER MESSAGE
# ============================================================

async def _forward_customer_message(
    user,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    topic_id: int,
    selected_game: str,
    _is_retry: bool = False,
) -> bool:

    try:

        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=topic_id,
            text=(
                "💬 CUSTOMER MESSAGE\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Name     : {user.full_name or user.first_name or 'Unknown'}\n"
                f"🔗 Username : @{user.username or 'No username'}\n"
                f"🆔 ID       : {user.id}\n"
                f"🎮 Game     : {selected_game}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        )

        await context.bot.copy_message(
            chat_id=SUPPORT_GROUP_ID,
            from_chat_id=user.id,
            message_id=update.message.message_id,
            message_thread_id=topic_id
        )

        logger.info(
            "Customer %s → topic %s | game: %s",
            user.id,
            topic_id,
            selected_game
        )

        return True

    except TelegramError as e:

        if _is_closed_topic_error(e) and not _is_retry:

            logger.warning(
                "Topic %s closed — recreating for user %s",
                topic_id,
                user.id
            )

            new_topic_id = await _clear_topic_and_recreate(
                user,
                topic_id,
                context
            )

            if not new_topic_id:
                return False

            return await _forward_customer_message(
                user=user,
                update=update,
                context=context,
                topic_id=new_topic_id,
                selected_game=selected_game,
                _is_retry=True,
            )

        logger.error("Message forward failed: %s", e)

        return False


# ============================================================
# START
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

        await update.message.reply_text(
            f"👋 Welcome to WishWheel Support!\n\n"
            f"🎮 Game selected: {pre_selected_game}\n\n"
            "💬 Send your message — "
            "our team will assist you right away!"
        )

    else:

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

    db_upsert_user(
        user_id=user.id,
        name=user.full_name or user.first_name or "Unknown",
        username=user.username or "",
        topic_id=topic_id,
        game=game,
    )

    db_add_topic_mapping(topic_id, user.id)

    try:

        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=topic_id,
            text=f"🎮 GAME SELECTED: {game}"
        )

    except TelegramError as e:

        if _is_closed_topic_error(e):

            logger.warning(
                "Game selection — closed topic %s, recreating",
                topic_id
            )

            new_topic_id = await _clear_topic_and_recreate(
                user,
                topic_id,
                context
            )

            if new_topic_id:

                db_upsert_user(
                    user_id=user.id,
                    name=user.full_name or user.first_name or "Unknown",
                    username=user.username or "",
                    topic_id=new_topic_id,
                    game=game,
                )

                db_add_topic_mapping(new_topic_id, user.id)

                try:

                    await context.bot.send_message(
                        chat_id=SUPPORT_GROUP_ID,
                        message_thread_id=new_topic_id,
                        text=f"🎮 GAME SELECTED: {game}"
                    )

                except TelegramError as e2:

                    logger.error(
                        "Game notify failed after recreate: %s",
                        e2
                    )

        else:

            logger.error("Game notify failed: %s", e)

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

    row = db_get_user(user.id)

    selected_game = (
        row["game"]
        if row and row["game"]
        else "Not selected"
    )

    success = await _forward_customer_message(
        user=user,
        update=update,
        context=context,
        topic_id=topic_id,
        selected_game=selected_game,
    )

    # Success pe kuch nahi — seamless chat feel
    if not success:

        await update.message.reply_text(
            "⚠️ Could not send message. Please try again."
        )


# ============================================================
# SUPPORT GROUP MESSAGE
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

    row = db_get_user_by_topic(topic_id)

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

    conn = get_db()

    with conn.cursor() as cur:

        cur.execute("SELECT COUNT(*) AS c FROM users")
        total = cur.fetchone()["c"]

        cur.execute(
            "SELECT COUNT(*) AS c FROM users "
            "WHERE created_at >= NOW() - INTERVAL '1 day'"
        )
        new_today = cur.fetchone()["c"]

        cur.execute(
            "SELECT COUNT(*) AS c FROM users "
            "WHERE topic_id IS NOT NULL"
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
            "Example:\n"
            "/broadcast Naya game add ho gaya!"
        )

        return

    all_users = db_get_all_users()

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

            await context.bot.send_message(
                chat_id=row["user_id"],
                text=f"📢 WishWheel Update\n\n{broadcast_text}"
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

    row = db_get_user_by_topic(topic_id)

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

    row = db_get_user_by_topic(topic_id)

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

        db_clear_user_topic(customer_id, topic_id)

        await message.reply_text(
            "✅ Topic closed.\n"
            "Next customer message will create a fresh topic."
        )

        logger.info(
            "Topic %s closed | customer %s cleared",
            topic_id,
            customer_id
        )

    except TelegramError as e:

        logger.error("Topic close failed: %s", e)

        await message.reply_text(
            f"⚠️ Error closing topic: {e}"
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

    init_db()

    print(f"Support Group   : {SUPPORT_GROUP_ID}")
    print(f"Authorized Staff: {AUTHORIZED_STAFF}")
    print("=" * 42)

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("games", games)
    )

    application.add_handler(
        CommandHandler("support", support)
    )

    application.add_handler(
        CallbackQueryHandler(
            game_selected,
            pattern=r"^game:"
        )
    )

    application.add_handler(
        CommandHandler(
            "id",
            customer_info,
            filters=filters.Chat(chat_id=SUPPORT_GROUP_ID)
        )
    )

    application.add_handler(
        CommandHandler(
            "close",
            close_topic,
            filters=filters.Chat(chat_id=SUPPORT_GROUP_ID)
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats,
            filters=filters.Chat(chat_id=SUPPORT_GROUP_ID)
        )
    )

    application.add_handler(
        CommandHandler(
            "broadcast",
            broadcast,
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
            filters.Chat(chat_id=SUPPORT_GROUP_ID)
            & ~filters.COMMAND,
            support_group_message
        )
    )

    application.add_error_handler(error_handler)

    print("Bot is running...")
    print("=" * 42)

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()