"""WishWheel Telegram support + lightweight CRM.

Additive replacement for the original single-file bot. Existing Telegram customer
IDs, topic IDs and old database tables remain intact.
"""
import asyncio
import logging
import re
from decimal import Decimal, InvalidOperation
from contextlib import asynccontextmanager
from types import SimpleNamespace

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError, RetryAfter
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)
from psycopg2 import IntegrityError
from settings import BRAND, Config, GAMES, BONUS_OPTIONS, LABELS
from crm import (parse_game, parse_source, normalize_game, normalize_label,
                 parse_account_template,
                 normalize_workflow, safe_username, topic_title, profile_text,
                 retry_delay, is_permanent_error, is_topic_missing, short)
from database import Database

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
log = logging.getLogger(__name__)


class State:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.roles = {}
        self.tasks = []
        self._user_locks = {}
        self._lock_guard = asyncio.Lock()
        self.delivery_wakeup = asyncio.Event()

    @asynccontextmanager
    async def lock(self, user_id):
        # Ref counting prevents the original cleanup race: no waiting task can
        # hold a lock object which has been removed from the dictionary.
        async with self._lock_guard:
            entry = self._user_locks.get(user_id)
            if entry is None:
                entry = [asyncio.Lock(), 0]
                self._user_locks[user_id] = entry
            entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            async with self._lock_guard:
                entry[1] -= 1
                if entry[1] == 0 and self._user_locks.get(user_id) is entry:
                    self._user_locks.pop(user_id, None)


def state(context):
    return context.application.bot_data['state']


def in_group(update, s):
    return bool(update.effective_chat and update.effective_chat.id == s.cfg.group_id)


def in_private(update):
    return bool(update.effective_chat and update.effective_chat.type == 'private')


def owner(update, s):
    return bool(update.effective_user and update.effective_user.id in s.cfg.owner_ids)


def staff_role(update, s):
    """Resolve saved and Railway staff independently of optional role labels."""
    uid = update.effective_user.id if update.effective_user else None
    if uid is None:
        return None
    if uid in s.cfg.owner_ids:
        return 'owner'
    if uid in s.cfg.seed_staff:
        return 'staff'
    role = s.roles.get(uid)
    if role is None:
        return None
    # roles() queries only active staff. Historical role labels remain authorized.
    return role if role in ('owner', 'admin', 'staff') else 'staff'


async def staff_access(update, context):
    """One canonical access check for commands, callbacks and forum replies."""
    s = state(context)
    if not update.effective_user or not in_group(update, s):
        return None
    role = staff_role(update, s)
    if role:
        return role
    try:
        s.roles = await s.db.roles()
    except Exception as exc:
        log.warning('Could not refresh staff roles for id=%s: %s',
                    update.effective_user.id, exc)
    role = staff_role(update, s)
    if role:
        return role
    try:
        member = await context.bot.get_chat_member(
            chat_id=s.cfg.group_id, user_id=update.effective_user.id)
        status = getattr(member.status, 'value', member.status)
        if str(status).lower() in ('administrator', 'creator', 'owner'):
            return 'admin'
    except Exception as exc:
        log.info('Telegram admin lookup unavailable user=%s: %s',
                 update.effective_user.id, exc)
    return None


async def authorize(update, context, owner_only=False):
    s = state(context)
    message = update.effective_message
    if not message or not in_group(update, s):
        return False
    role = await staff_access(update, context)
    if not role:
        uid = update.effective_user.id if update.effective_user else 'unknown'
        if getattr(message, 'sender_chat', None):
            await message.reply_text(
                '⛔ Anonymous admin mode hides your personal Telegram ID. '
                'Post as your own account, then retry /staffcheck.')
        else:
            await message.reply_text(
                f'⛔ Staff access not found for Telegram ID {uid}. '
                'Send /staffcheck here to see exactly what is missing.')
        log.warning('Staff authorization rejected: user_id=%s chat_id=%s; '
                    'configured_ids=%d active_db_staff=%d',
                    uid, update.effective_chat.id,
                    len(s.cfg.owner_ids | s.cfg.seed_staff), len(s.roles))
        return False
    return True


async def staffcheck(update, context):
    """Self-service, non-privileged staff authorization diagnostics."""
    s = state(context)
    msg = update.effective_message
    if not msg or not in_group(update, s):
        return
    actor = update.effective_user
    uid = actor.id if actor else None
    role = await staff_access(update, context)
    sources = getattr(s.cfg, 'staff_id_sources', {}).get(uid, ())
    in_db = s.roles.get(uid) if uid is not None else None
    anonymous = bool(getattr(msg, 'sender_chat', None))
    await msg.reply_text(
        f'🔎 STAFF ACCESS CHECK\n'
        f'👤 Telegram ID: {uid or "unknown"}\n'
        f'📍 Group: {update.effective_chat.id}\n'
        f'🗄️ Active DB role: {in_db or "not found"}\n'
        f'⚙️ Railway ID variable(s): {", ".join(sorted(sources)) or "not found"}\n'
        f'🛡️ Effective permission: {role or "NOT AUTHORIZED"}\n'
        + ('⚠️ You are posting anonymously. Turn anonymous admin off.\n'
           if anonymous else '')
        + ('Ask a staff admin to use /addstaff with this Telegram ID, '
           'or add it to a Railway staff ID variable and redeploy.'
           if not role else '✅ Broadcast and CRM commands are enabled.'))


async def topic_customer(update, context, owner_only=False):
    if not await authorize(update,context,owner_only):
        return None
    mid = update.effective_message.message_thread_id
    if not mid or mid == 1:
        await update.effective_message.reply_text('⚠️ Use this command inside a customer forum topic.')
        return None
    row = await state(context).db.by_topic(mid)
    if not row:
        await update.effective_message.reply_text('⚠️ No customer mapped to this topic.')
    return row


def game_keyboard():
    keys = []
    for i in range(0, len(GAMES), 2):
        keys.append([InlineKeyboardButton(f'🎮 {x}', callback_data=f'game:{x}')
                     for x in GAMES[i:i+2]])
    return InlineKeyboardMarkup(keys)


def bonus_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(v, callback_data=f'bonus:{k}')]
        for k, v in BONUS_OPTIONS.items()
    ])


async def _update_profile_locked(s, bot, uid):
    row = await s.db.user(uid)
    if not row or not row['topic_id']:
        return
    topic = int(row['topic_id'])
    parts = await s.db.profile_parts(uid)
    body = profile_text(row,*parts)
    try:
        await bot.edit_forum_topic(s.cfg.group_id,topic,name=topic_title(row))
    except TelegramError as exc:
        if 'not modified' not in str(exc).lower():
            log.warning('Topic title update failed user=%s: %s',uid,exc)
    pid = row['profile_message_id']
    if pid:
        try:
            await bot.edit_message_text(body, chat_id=s.cfg.group_id,message_id=int(pid))
            return
        except TelegramError as exc:
            if 'not modified' in str(exc).lower():
                return
            log.warning('Profile edit failed user=%s: %s',uid,exc)
    # On fresh topic or deleted original profile, create a new pin.
    try:
        msg = await bot.send_message(chat_id=s.cfg.group_id,message_thread_id=topic,text=body)
        await s.db.profile_id(uid,msg.message_id)
        try:
            await bot.pin_chat_message(chat_id=s.cfg.group_id,message_id=msg.message_id,disable_notification=True)
        except TelegramError as exc:
            log.warning('Profile pin failed user=%s (bot needs pin permission): %s',uid,exc)
    except TelegramError as exc:
        log.warning('Profile creation failed user=%s: %s',uid,exc)


async def refresh_profile(s, bot, uid):
    async with s.lock(uid):
        await _update_profile_locked(s,bot,uid)


async def _create_topic_locked(user,s,bot):
    row = await s.db.user(user.id)
    topic = await bot.create_forum_topic(chat_id=s.cfg.group_id,name=topic_title(row))
    topic_id = int(topic.message_thread_id)
    try:
        await s.db.assign_topic(user.id, topic_id)
    except Exception:
        try:
            await bot.delete_forum_topic(chat_id=s.cfg.group_id,message_thread_id=topic_id)
        except TelegramError:
            log.exception('Orphan topic cleanup failed: %s',topic_id)
        raise
    await _update_profile_locked(s,bot,user.id)
    log.info('Created topic user=%s topic=%s',user.id,topic_id)
    return topic_id


async def _recover_topic_locked(user,s,bot,row):
    tid = int(row['topic_id'])
    try:
        await bot.reopen_forum_topic(chat_id=s.cfg.group_id,message_thread_id=tid)
    except TelegramError as exc:
        msg=str(exc).lower()
        if 'not modified' in msg or 'not closed' in msg:
            pass
        elif is_topic_missing(exc) and 'closed' not in msg:
            log.warning('Topic not found, recreating user=%s topic=%s',user.id,tid)
            return await _create_topic_locked(user,s,bot)
        else:
            # Rate limits / permissions / API outages MUST NOT orphan topics.
            log.warning('Topic reopen deferred user=%s topic=%s: %s',user.id,tid,exc)
            raise
    await s.db.mark_topic(user.id,'open')
    return tid


async def get_topic(user,context,game=None,source=None,bonus=None):
    s=state(context)
    async with s.lock(user.id):
        row=await s.db.upsert_user(user,game=game,source=source,bonus=bonus)
        if row.get('is_blocked'):
            raise BlockedCustomer(f'Customer {user.id} is blocked')
        if row['topic_id']:
            if row['topic_status']=='closed':
                result=await _recover_topic_locked(user,s,context.bot,row)
            else:
                result=int(row['topic_id'])
            if game or source or bonus or not row['profile_message_id']:
                await _update_profile_locked(s,context.bot,user.id)
            return result
        return await _create_topic_locked(user,s,context.bot)


async def recover_after_delivery(s,bot,uid):
    row = await s.db.user(uid)
    if not row:
        return None
    if not row['topic_id']:
        return None
    async with s.lock(uid):
        row = await s.db.user(uid)
        tid = int(row['topic_id'])
        try:
            await bot.reopen_forum_topic(chat_id=s.cfg.group_id,message_thread_id=tid)
            await s.db.mark_topic(uid,'open')
            return tid
        except TelegramError as exc:
            msg=str(exc).lower()
            if 'not closed' in msg:
                await s.db.mark_topic(uid,'open')
                return tid
            if is_topic_missing(exc) and 'closed' not in msg:
                # Regenerate a missing forum topic using the stored user identity.
                saved_user=SimpleNamespace(id=uid,full_name=row['name'],first_name=row['name'],
                                           username=row['username'])
                return await _create_topic_locked(saved_user,s,bot)
            log.warning('Delivery topic recovery deferred user=%s topic=%s: %s',uid,tid,exc)
            raise


async def deliver_job(application, s, job):
    bot=application.bot
    uid=int(job['user_id'])
    row=await s.db.user(uid)
    if not row:
        return True
    direction=job['direction']
    if row.get('is_blocked'):
        return True  # Cancel queued copies and messages once customer is blocked.
    if direction=='broadcast' and (row.get('marketing_opted_out') or row.get('bot_blocked')):
        return True  # Preserve explicit stop requests and avoid unreachable players
    if direction=='customer':
        tid=row['topic_id']
        if not tid:
            raise RuntimeError('Customer topic has not been mapped')
        try:
            await bot.copy_message(chat_id=s.cfg.group_id,message_thread_id=int(tid),
                                   from_chat_id=int(job['source_chat_id']),
                                   message_id=int(job['source_message_id']))
        except TelegramError as exc:
            if is_topic_missing(exc):
                tid=await recover_after_delivery(s,bot,uid)
                if tid:
                    await bot.copy_message(chat_id=s.cfg.group_id,message_thread_id=tid,
                                           from_chat_id=int(job['source_chat_id']),
                                           message_id=int(job['source_message_id']))
                else:
                    raise
            else:
                raise
    elif direction=='staff':
        await bot.copy_message(chat_id=uid,from_chat_id=int(job['source_chat_id']),
                               message_id=int(job['source_message_id']))
    else:
        await bot.send_message(chat_id=uid,text=job['body'])
    return True


def unreachable_player(exc):
    text=str(exc).casefold()
    return any(term in text for term in (
        'bot was blocked by the user', 'bot is blocked by the user',
        'user is deactivated', 'chat not found', 'user not found'))


async def mirror_one_broadcast(application,s):
    """Copy delivered messages to staff without ever resending to the customer."""
    job=await s.db.outbox_claim_mirror()
    if job is None:
        return False
    job_id=int(job['id'])
    if not job.get('topic_id') or job.get('topic_status')=='closed':
        await s.db.outbox_mirror_result(job_id,False,skipped=True)
        return True
    try:
        await application.bot.send_message(
            chat_id=s.cfg.group_id, message_thread_id=int(job['topic_id']),
            text=('📢 BROADCAST SENT\n\n'+(job.get('body') or ''))[:4000])
    except RetryAfter as exc:
        await s.db.outbox_mirror_result(job_id,False,str(exc),max(1,int(exc.retry_after)+1))
    except Exception as exc:
        attempt=int(job.get('attempts') or 0)+1
        retry=0 if is_permanent_error(exc) or is_topic_missing(exc) or attempt>=s.cfg.max_delivery_attempts else retry_delay(attempt)
        await s.db.outbox_mirror_result(job_id,False,str(exc),retry,
                                         skipped=is_topic_missing(exc))
        log.warning('Broadcast support topic mirror failed job=%s attempt=%d: %s',job_id,attempt,exc)
    else:
        await s.db.outbox_mirror_result(job_id,True)
    return True


async def outbox_worker(application,s):
    log.info('Persistent outbox worker started')
    while True:
        try:
            job=await s.db.outbox_claim()
            if job is None:
                if await mirror_one_broadcast(application,s):
                    continue
                try:
                    await asyncio.wait_for(s.delivery_wakeup.wait(),timeout=1)
                except asyncio.TimeoutError:
                    pass
                s.delivery_wakeup.clear()
                continue
            attempt=int(job['attempts'])+1
            try:
                await deliver_job(application,s,job)
            except RetryAfter as exc:
                delay=max(1,int(exc.retry_after)+1)
                await s.db.outbox_result(job['id'],False,str(exc),delay)
                log.warning('Delivery rate limited job=%s retry=%ss',job['id'],delay)
            except Exception as exc:
                permanent=is_permanent_error(exc)
                retry=0 if permanent or attempt>=s.cfg.max_delivery_attempts else retry_delay(attempt)
                if job['direction']=='broadcast' and unreachable_player(exc):
                    try:
                        await s.db.mark_bot_blocked(int(job['user_id']),True)
                    except Exception:
                        log.exception('Failed to record bot-blocked customer %s',job['user_id'])
                await s.db.outbox_result(job['id'],False,str(exc),retry)
                log.warning('Delivery failed job=%s direction=%s attempt=%s retry=%s: %s',
                            job['id'],job['direction'],attempt,retry,exc)
                if not retry and job['direction']=='customer':
                    try:
                        await application.bot.send_message(chat_id=int(job['user_id']),
                            text='⚠️ Your earlier message could not reach support. Please send it again.')
                    except TelegramError:
                        pass
                if not retry and job['direction'] in ('staff','shortcut'):
                    try:
                        row=await s.db.user(job['user_id'])
                        if row and row['topic_id']:
                            await application.bot.send_message(chat_id=s.cfg.group_id,
                               message_thread_id=row['topic_id'],
                               text=f'⚠️ Delivery failed for message/job {job["id"]}: {short(exc,150)}')
                    except TelegramError:
                        pass
            else:
                await s.db.outbox_result(job['id'],True)
                if job['direction']=='broadcast':
                    await mirror_one_broadcast(application,s)
            await asyncio.sleep(s.cfg.broadcast_gap if job['direction']=='broadcast' else 0.4)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Outbox worker crashed during iteration; recovering')
            await asyncio.sleep(2)


async def retention_worker(application,s):
    if not s.cfg.retention_days:
        return
    while True:
        try:
            for row in await s.db.old_customers(s.cfg.retention_days):
                try:
                    if row['topic_id']:
                        await application.bot.delete_forum_topic(chat_id=s.cfg.group_id,message_thread_id=int(row['topic_id']))
                    await s.db.delete_user(row['user_id'])
                except TelegramError:
                    # Fail closed: don't delete DB customer metadata while forum copies remain.
                    log.exception('Retention topic deletion failed user=%s',row['user_id'])
                except Exception:
                    log.exception('Retention DB cleanup failed user=%s',row['user_id'])
            await asyncio.sleep(24*3600)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Retention cycle error')
            await asyncio.sleep(3600)


async def enqueue_incoming(s,uid,direction,message):
    jid=await s.db.outbox_enqueue(uid,direction,message.chat.id,message.message_id)
    if jid:
        s.delivery_wakeup.set()
    return jid



class BlockedCustomer(Exception):
    """Customer is suspended from bot-managed support messages."""


async def _reject_blocked(update, context):
    user = update.effective_user
    if not user:
        return False
    customer = await state(context).db.user(user.id)
    if customer and customer.get('is_blocked'):
        notice = '⛔ Your support access is currently blocked. Contact the support team to request a review.'
        if update.callback_query:
            await update.callback_query.answer(notice, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(notice)
        return True
    return False


async def customer_start(update,context):
    if not in_private(update):
        return
    if await _reject_blocked(update,context): return
    user=update.effective_user
    game=parse_game(context.args or [])
    source=parse_source(context.args or [])
    try:
        # A new /start activates ordinary bot updates without a separate /optin step.
        # Legacy customers with marketing_opt_in=False are not silently re-enrolled:
        # that historic value could represent a previous explicit /optout.
        s=state(context)
        previous=await s.db.user(user.id)
        await get_topic(user,context,game=game,source=source)
        if previous is None:
            await s.db.opt_in(user.id,True)
    except Exception:
        log.exception('Customer start failed user=%s',user.id)
        await update.message.reply_text('⚠️ Support is temporarily unavailable. Please try again shortly.')
        return
    if game:
        await update.message.reply_text(f'👋 Welcome to {BRAND} Support!\n🎮 Game selected: {game}\nSend your message below.')
    else:
        await update.message.reply_text(f'👋 Welcome to {BRAND} Support!\n🎁 Choose your bonus:',reply_markup=bonus_keyboard())


async def help_command(update,context):
    if not in_private(update):
        return
    await update.message.reply_text(
        f'{BRAND} SUPPORT\n/start — Begin support\n/games — Choose a game\n'
        '/support — Contact staff\n/privacy — Privacy policy\n/delete_data — Request account deletion\n'
        'Or simply send a message here.')


async def game_list(update,context):
    if not in_private(update):
        return
    if await _reject_blocked(update,context): return
    await get_topic(update.effective_user,context)
    await update.message.reply_text('🎮 Choose your game:',reply_markup=game_keyboard())


async def support(update,context):
    if not in_private(update):
        return
    if await _reject_blocked(update,context): return
    await get_topic(update.effective_user,context)
    await update.message.reply_text('💬 Send your message here. You can also choose a game:',reply_markup=game_keyboard())


async def customer_choice(update,context):
    query=update.callback_query
    if not query or not in_private(update):
        return
    if await _reject_blocked(update,context): return
    await query.answer()
    u=update.effective_user
    if query.data.startswith('bonus:'):
        bonus=BONUS_OPTIONS.get(query.data[6:])
        if not bonus:
            await query.message.reply_text('Invalid bonus selection.')
            return
        await get_topic(u,context,bonus=bonus)
        await query.message.reply_text(f'✅ {bonus} selected. Choose a game:',reply_markup=game_keyboard())
    elif query.data.startswith('game:'):
        game=parse_game([query.data[5:]])
        if not game:
            await query.message.reply_text('Invalid game selection.')
            return
        await get_topic(u,context,game=game)
        await query.message.reply_text(f'✅ {game} selected. Send your message here.')


async def customer_message(update,context):
    if not in_private(update) or not update.message:
        return
    if await _reject_blocked(update,context): return
    user=update.effective_user
    try:
        await get_topic(user,context)
        if await enqueue_incoming(state(context),user.id,'customer',update.message) is None:
            log.info('Duplicate customer update ignored user=%s msg=%s',user.id,update.message.message_id)
    except Exception:
        log.exception('Customer message processing failed user=%s',user.id)
        await update.message.reply_text('⚠️ Message could not be saved. Please try again.')


async def group_message(update,context):
    s=state(context)
    msg=update.message
    if not msg or not in_group(update,s) or not msg.from_user or msg.from_user.is_bot:
        return
    if not await staff_access(update,context):
        log.warning('Ignored unauthorized group message from id=%s',msg.from_user.id)
        return
    tid=msg.message_thread_id
    if not tid or tid==1:
        return
    row=await s.db.by_topic(tid)
    if not row:
        return
    uid=int(row['user_id'])
    try:
        await enqueue_incoming(s,uid,'staff',msg)
    except Exception:
        log.exception('Could not queue staff message id=%s',msg.message_id)
        await msg.reply_text('⚠️ Message NOT queued; please retry.')
        return
    # Capture only outbound human-staff messages that have BOTH Username and
    # Password lines and a recognizable suffix or an explicit Game: line.
    # The normal staff message still goes to the player through the outbox.
    account=parse_account_template(msg.text or msg.caption or '')
    if account:
        try:
            duplicates=await s.db.set_account(
                uid,account.game,account.username,msg.from_user.id,
                password=account.password,source_chat=msg.chat.id,source_message=msg.message_id)
            if duplicates is not None:  # Skip repeated delivery of the same Telegram update.
                await refresh_profile(s,context.bot,uid)
                acknowledgement=f'✅ Auto-saved {account.game}: {account.username} (password stored)'
                if duplicates:
                    acknowledgement+='\n⚠️ Another customer has the same game account.'
                await msg.reply_text(acknowledgement)
        except Exception:
            log.exception('Account auto-save failed group_message_id=%s',msg.message_id)
            await msg.reply_text('⚠️ Account message queued, but automatic account saving failed. You can retry with /setaccount.')


async def show_profile(update,context):
    row=await topic_customer(update,context)
    if not row:
        return
    s=state(context)
    parts=await s.db.profile_parts(row['user_id'])
    await update.message.reply_text(profile_text(row,*parts))


async def edit_profile(update,context,action):
    row=await topic_customer(update,context,owner_only=(action=='deposit'))
    if not row:
        return
    s=state(context)
    args=context.args or []
    uid=int(row['user_id'])
    actor=update.effective_user.id
    try:
        if action=='setaccount':
            # Both /setaccount Juwa player123 009988 and
            # /setaccount Cash Master | player123 | 009988 work.
            raw=' '.join(args).strip()
            fields=[v.strip() for v in raw.split('|')] if '|' in raw else args
            if len(fields) not in (2,3) or not all(fields):
                raise ValueError('Usage: /setaccount <Game> <username> [password] OR /setaccount <Game Name> | <username> | <password>')
            game=normalize_game(fields[0]); account=safe_username(fields[1])
            password=fields[2] if len(fields)==3 else None
            if password is not None and (len(password)>256 or any(ord(c)<32 for c in password)):
                raise ValueError('Password must contain 1-256 printable characters')
            duplicates=await s.db.set_account(uid,game,account,actor,password=password)
            message=f'✅ Saved {game} account: {account}'
            if duplicates:
                message+=f'\n⚠️ Same game account is linked to customer IDs: {", ".join(map(str,duplicates))}'
        elif action=='delaccount':
            if not args:
                raise ValueError('Usage: /delaccount <Game Name>')
            game=normalize_game(' '.join(args))
            if not await s.db.delete_account(uid,game,actor):
                await update.message.reply_text('Account was not found.')
                return
            message=f'✅ Removed saved {game} account.'
        elif action in ('label','unlabel'):
            if not args:
                raise ValueError(f'Usage: /{action} <any label text> — e.g. /label High Roller')
            custom=' '.join(args)
            label=normalize_label(custom)
            await s.db.set_label(uid,label,actor,remove=(action=='unlabel'),display=custom)
            message=f'✅ Label {custom.strip()} {"removed" if action=="unlabel" else "saved"}.'
        elif action=='note':
            body=' '.join(args).strip()
            if not body or len(body)>500:
                raise ValueError('Usage: /note <text> (maximum 500 characters)')
            await s.db.note(uid,body,actor)
            message='✅ Staff note saved (visible only to authorized staff).'
        elif action=='status':
            if not args:
                raise ValueError('Usage: /status <any status name>')
            value=normalize_workflow(' '.join(args)); await s.db.workflow(uid,value,actor)
            message=f'✅ Workflow status updated: {value}'
        elif action=='assign':
            if len(args)!=1 or (args[0].lower()!='none' and not args[0].isdigit()):
                raise ValueError('Usage: /assign <authorized_staff_id|none>')
            staff_id=None if args[0].lower()=='none' else int(args[0])
            if staff_id and staff_id not in s.roles and staff_id not in s.cfg.owner_ids:
                raise ValueError('That ID is not an active authorized staff member.')
            await s.db.assign(uid,staff_id,actor)
            message=f'✅ Assigned to {staff_id or "nobody"}.'
        elif action=='deposit':
            if len(args)!=2:
                raise ValueError('Usage: /deposit <amount> <unique_reference> (owner only)')
            amount=Decimal(args[0]); ref=short(args[1],100)
            if not amount.is_finite() or amount<=0 or amount>100000000 or len(ref)<3:
                raise ValueError('Enter positive amount and a reference (at least 3 characters).')
            await s.db.deposit(uid,amount,ref,actor)
            message=f'✅ Staff-entered payment record saved: ${amount:.2f}, ref {ref}.\n💰 Depositor label added automatically. This is not automatic payment verification.'
        else:
            raise ValueError('Unknown edit command')
    except (ValueError, InvalidOperation) as exc:
        await update.message.reply_text(f'⚠️ {exc}')
        return
    except IntegrityError:
        await update.message.reply_text('⚠️ Database rejected this value (possibly duplicate payment reference).')
        return
    await refresh_profile(s,context.bot,uid)
    await update.message.reply_text(message)


async def cmd_setaccount(u,c): await edit_profile(u,c,'setaccount')
async def cmd_delaccount(u,c): await edit_profile(u,c,'delaccount')
async def cmd_label(u,c): await edit_profile(u,c,'label')
async def cmd_unlabel(u,c): await edit_profile(u,c,'unlabel')
async def cmd_note(u,c): await edit_profile(u,c,'note')
async def cmd_status(u,c): await edit_profile(u,c,'status')
async def cmd_assign(u,c): await edit_profile(u,c,'assign')
async def cmd_deposit(u,c): await edit_profile(u,c,'deposit')



async def set_customer_block(update,context,blocked):
    """Block or unblock an existing customer from their topic or by Telegram ID."""
    if not await authorize(update,context): return
    s=state(context)
    args=list(context.args or [])
    uid=None
    if args and args[0].isdigit() and int(args[0])>0:
        uid=int(args.pop(0))
    else:
        mid=update.effective_message.message_thread_id
        if mid and mid!=1:
            mapped=await s.db.by_topic(mid)
            if mapped: uid=int(mapped['user_id'])
    if not uid:
        await update.message.reply_text('Usage in a customer topic: /block [reason] or /unblock\nAnywhere in support group: /block <telegram_id> [reason]')
        return
    row=await s.db.user(uid)
    if not row:
        await update.message.reply_text(f'Customer {uid} not found in this bot.')
        return
    reason=' '.join(args).strip()[:250]
    if bool(row.get('is_blocked'))==blocked:
        await update.message.reply_text(f'ℹ️ Customer {uid} is already {"blocked" if blocked else "unblocked"}.')
        return
    ok=await s.db.set_blocked(uid,blocked,update.effective_user.id,reason)
    if not ok:
        await update.message.reply_text('Could not update the customer. Please retry.')
        return
    try:
        await refresh_profile(s,context.bot,uid)
    except Exception:
        log.exception('Could not refresh block status in profile user=%s',uid)
    await update.message.reply_text(
        f'{"⛔ Blocked" if blocked else "✅ Unblocked"} customer {uid}. '
        + ('New inbound messages and queued outgoing deliveries are disabled.' if blocked
           else 'Support messaging can resume.') +
        (f'\nReason: {reason}' if reason else ''))


async def block_customer(update,context):
    await set_customer_block(update,context,True)


async def unblock_customer(update,context):
    await set_customer_block(update,context,False)


async def show_accounts(update,context):
    """Detailed view; pinned profiles intentionally only show game usernames."""
    row=await topic_customer(update,context)
    if not row:
        return
    accounts=await state(context).db.account_details(int(row['user_id']))
    if not accounts:
        await update.message.reply_text('No game accounts saved yet.')
        return
    lines=['🔐 SAVED GAME ACCOUNTS — '+short(row['name'],60)]
    for a in accounts:
        lines += [f"\n🎮 {a['game']}",f"👤 Username: {a['account_username']}",
                  f"🔑 Password: {a['account_password'] if a['account_password'] is not None else '(not saved)'}"]
    await update.message.reply_text('\n'.join(lines)[:3950])


async def find_account(update,context):
    if not await authorize(update,context): return
    if not (context.args or []):
        await update.message.reply_text('Usage: /findaccount <game_username>')
        return
    term=' '.join(context.args).strip().lstrip('@')
    matches=await state(context).db.find_account(term)
    if not matches:
        await update.message.reply_text('No saved account matches.')
        return
    lines=[f'🔎 {short(term,80)} — saved account matches:']
    for r in matches:
        lines.append(f'{r["game"]}: {r["account_username"]} | {short(r["name"],50)} (ID {r["user_id"]}, topic {r["topic_id"]})')
    await update.message.reply_text('\n'.join(lines)[:3950])


async def show_history(update,context):
    row=await topic_customer(update,context)
    if not row: return
    records=await state(context).db.history(row['user_id'])
    content='\n'.join(f'{x["created_at"]:%Y-%m-%d %H:%M} | {x["action"]} | staff {x["actor_id"]}: {short(x["detail"],80)}' for x in records)
    await update.message.reply_text('📜 CUSTOMER CHANGES\n'+(content or 'No changes recorded yet.'))


async def close_topic(update,context):
    row=await topic_customer(update,context)
    if not row: return
    s=state(context); uid=int(row['user_id']); tid=int(row['topic_id'])
    async with s.lock(uid):
        await update.message.reply_text('✅ Closing topic. New customer messages will reopen it.')
        try:
            await context.bot.close_forum_topic(chat_id=s.cfg.group_id,message_thread_id=tid)
        except TelegramError as exc:
            await update.message.reply_text(f'⚠️ Could not close Telegram topic: {short(exc)}')
            return
        await s.db.mark_topic(uid,'closed')
    try:
        await context.bot.send_message(chat_id=uid,text=f'✅ Your {BRAND} support chat has been closed. Send a message anytime to reopen it.')
    except TelegramError:
        pass


async def stats(update,context):
    if not await authorize(update,context): return
    main,games,labels=await state(context).db.stats()
    lines=[f'📊 {BRAND} Statistics',f'Total: {main["total"]} | New today: {main["today"]} | Open topics: {main["open"]}',
           '\nGames:']+[f'{x["game"]}: {x["c"]}' for x in games]+['\nPrimary labels:']+[f'{x["primary_label"]}: {x["c"]}' for x in labels]
    await update.message.reply_text('\n'.join(lines))


async def health(update,context):
    if not await authorize(update,context,owner_only=True): return
    counts=await state(context).db.outbox_counts()
    await update.message.reply_text('🩺 Delivery queue\n'+'\n'.join(f'{k}: {v}' for k,v in counts.items()))


async def staff_list(update,context):
    if not await authorize(update,context): return
    s=state(context)
    await update.message.reply_text('👥 Active staff\n'+'\n'.join(f'{uid}: {role}' for uid,role in sorted(s.roles.items())))


async def staff_modify(update,context,remove=False):
    if not await authorize(update,context,owner_only=True): return
    args=context.args or []
    if not args or not args[0].isdigit() or int(args[0])<=0:
        await update.message.reply_text('Usage: /addstaff <telegram_id> [name] or /removestaff <telegram_id>')
        return
    uid=int(args[0]); s=state(context)
    if uid in (s.cfg.owner_ids | s.cfg.seed_staff):
        await update.message.reply_text('Railway-configured IDs must be managed in environment variables.')
        return
    name=' '.join(args[1:])[:100] or 'Staff'
    await s.db.staff_set(uid,name,'staff',not remove)
    s.roles=await s.db.roles()
    await update.message.reply_text(f'✅ ID {uid} {"deactivated" if remove else "activated"}. Change persists across restarts.')


async def addstaff(update,context): await staff_modify(update,context,False)
async def removestaff(update,context): await staff_modify(update,context,True)


async def shortcut_list(update,context):
    if not await authorize(update,context): return
    data=await state(context).db.shortcuts()
    await update.message.reply_text('⚡ Shortcuts\n'+('\n'.join(f'/use {x["shortcut_key"]}: {short(x["body"],100)}' for x in data) or 'No shortcuts.'))


async def shortcut_change(update,context,remove=False):
    if not await authorize(update,context,owner_only=True): return
    args=context.args or []
    if not args or not re.fullmatch('[a-z0-9_]{1,25}',args[0].lower()):
        await update.message.reply_text('Usage: /addshortcut <key> <message> or /delshortcut <key>')
        return
    s=state(context); key=args[0].lower()
    if remove:
        found=await s.db.shortcut_delete(key)
        await update.message.reply_text('✅ Deleted.' if found else 'Not found.')
        return
    body=' '.join(args[1:]).strip()
    if not body or len(body)>3000:
        await update.message.reply_text('Shortcuts must contain 1-3000 characters.')
        return
    await s.db.shortcut_set(key,body,update.effective_user.id)
    await update.message.reply_text('✅ Saved shortcut '+key)


async def addshortcut(update,context): await shortcut_change(update,context,False)
async def delshortcut(update,context): await shortcut_change(update,context,True)


async def use_shortcut(update,context):
    row=await topic_customer(update,context)
    if not row: return
    args=context.args or []
    if len(args)!=1:
        await update.message.reply_text('Usage: /use <shortcut_key>')
        return
    s=state(context)
    text=await s.db.shortcut_get(args[0].lower())
    if not text:
        await update.message.reply_text('Shortcut not found. Try /shortcuts.')
        return
    text=text.replace('{name}',(row['name'] or 'there').split(' ')[0])
    key=f'shortcut:{s.cfg.group_id}:{update.message.message_id}'
    await s.db.outbox_enqueue(row['user_id'],'shortcut',body=text,key=key)
    s.delivery_wakeup.set()
    await update.message.reply_text('✅ Shortcut queued for delivery.')


async def privacy(update,context):
    if not in_private(update): return
    url=state(context).cfg.privacy_url
    await update.message.reply_text('🔒 Privacy policy: '+url if url else
        '🔒 Our support bot retains your messages in a private support group and stores your Telegram ID, support choices and optional game-account details. Contact support for the current public privacy policy. Use /delete_data to request deletion. Staff topic copies may require separate removal.')


async def delete_data(update,context):
    if not in_private(update): return
    await update.message.reply_text(
        '⚠️ Delete your customer profile, game accounts, notes, and payment records from this bot? This cannot be undone. The bot will attempt to delete its forum topic. Human-held copies may remain.',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('Delete my records',callback_data=f'delete:yes:{update.effective_user.id}'),
            InlineKeyboardButton('Cancel',callback_data=f'delete:no:{update.effective_user.id}')
        ]]))


async def delete_callback(update,context):
    q=update.callback_query
    if not q or not in_private(update): return
    parts=(q.data or '').split(':')
    if len(parts)!=3 or not parts[2].isdigit() or int(parts[2])!=update.effective_user.id:
        await q.answer('This confirmation belongs to a different customer.',show_alert=True)
        return
    await q.answer()
    if parts[1]=='no':
        await q.edit_message_text('✅ Deletion cancelled.')
        return
    s=state(context); uid=update.effective_user.id
    async with s.lock(uid):
        row=await s.db.user(uid)
        if not row:
            await q.edit_message_text('No active customer record found.')
            return
        if row['topic_id']:
            try:
                await context.bot.delete_forum_topic(chat_id=s.cfg.group_id,message_thread_id=int(row['topic_id']))
            except TelegramError as exc:
                # Keep records if we cannot remove the copied messages; manual action needed.
                log.warning('Customer deletion topic cleanup failed user=%s: %s',uid,exc)
                await q.edit_message_text('⚠️ Could not delete support topic. Staff must resolve this first. Your database record has NOT been deleted.')
                return
        await s.db.delete_user(uid)
    await q.edit_message_text('✅ Customer records were removed from the bot database and its support topic was deleted (if present). Other copies held outside this bot may remain.')


async def optout(update,context):
    if not in_private(update):return
    s=state(context)
    await s.db.upsert_user(update.effective_user)
    await s.db.opt_in(update.effective_user.id,False)
    await update.message.reply_text('✅ Optional messages turned off. Your support chat still works.')


async def broadcast(update,context):
    """Queue the registered customer audience immediately, with no opt-in prompt."""
    if not await authorize(update,context): return
    args=list(context.args or [])
    label=None
    if args and args[0].startswith('--label='):
        try: label=normalize_label(args.pop(0).split('=',1)[1])
        except ValueError as exc:
            await update.message.reply_text(str(exc));return
    body=' '.join(args).strip()
    if not body or len(body)>3000:
        await update.message.reply_text('Usage: /broadcast [--label=VIP] <message>')
        return
    s=state(context)
    bid,_=await s.db.broadcast_draft(update.effective_user.id,body,label)
    queued=await s.db.broadcast_confirm(bid,update.effective_user.id,True)
    if queued is None:
        await update.message.reply_text('⚠️ Could not queue broadcast. Please retry.')
        return
    s.delivery_wakeup.set()
    await update.message.reply_text(
        f'📡 Broadcast #{bid} queued for {queued} registered customers.\n'
        'Oldest/inactive chats first; minimum 1 second between customer deliveries.\n'
        f'Progress: /bstatus {bid}\n'
        'Check a player: /bcheck <telegram_id> '+str(bid))


async def broadcast_status(update,context):
    if not await authorize(update,context): return
    args=context.args or []
    if args and not args[0].isdigit():
        await update.message.reply_text('Usage: /bstatus [broadcast_id]');return
    report=await state(context).db.broadcast_report(int(args[0]) if args else None)
    if not report:
        await update.message.reply_text('No broadcast record found.');return
    counts=report['delivery']; mirrors=report['mirror']
    errors='\n'.join(
        f" • {e['user_id']}: {short(e.get('last_error') or 'Unknown',100)}"
        for e in report['errors'])
    await update.message.reply_text(
        f"📡 BROADCAST #{report['id']} ({report['status']})\n"
        f"📨 Total queued: {sum(counts.values())}\n"
        f"✅ Sent: {counts.get('sent',0)}\n"
        f"⏳ Pending: {counts.get('pending',0)}\n"
        f"🔄 Processing: {counts.get('processing',0)}\n"
        f"❌ Failed: {counts.get('dead',0)}\n"
        f"💬 Staff chat copies sent: {mirrors.get('sent',0)}\n"
        f"⚠️ Copy failures: {mirrors.get('dead',0)}"
        + (f"\nErrors:\n{errors}" if errors else ''))


async def broadcast_check(update,context):
    if not await authorize(update,context): return
    args=context.args or []
    if not args or not args[0].isdigit() or (len(args)>1 and not args[1].isdigit()):
        await update.message.reply_text('Usage: /bcheck <telegram_id> [broadcast_id]');return
    uid=int(args[0]);bid=int(args[1]) if len(args)>1 else None
    report=await state(context).db.broadcast_check(uid,bid)
    if not report:
        await update.message.reply_text('That player is not registered in this bot.');return
    player=report['user'];delivery=report['delivery']
    body=(f'🔎 PLAYER {uid}\n'
          f"👤 {short(player.get('name') or 'Unknown',75)}\n"
          f"🚫 Staff block: {bool(player.get('is_blocked'))}\n"
          f"🚫 Telegram unreachable: {bool(player.get('bot_blocked'))}\n")
    if delivery:
        body+=(f"📨 Delivery: {delivery['state']}\n"
               f"🔁 Attempts: {delivery.get('attempts',0)}\n"
               f"💬 Staff copy: {delivery.get('mirror_state') or 'not yet'}\n"
               f"⚠️ Error: {short(delivery.get('last_error') or '-',200)}")
    else:
        body+='📨 No matching broadcast was queued for this player.'
    await update.message.reply_text(body)


async def broadcast_callback(update,context):
    q=update.callback_query
    if not q or not in_group(update,state(context)):
        return
    s=state(context)
    if not await staff_access(update,context):
        await q.answer('Only authorized staff can confirm. Use /staffcheck.',show_alert=True);return
    parts=(q.data or '').split(':')
    if len(parts)!=3 or not parts[2].isdigit():return
    bid=int(parts[2]);ok=parts[1]=='yes'
    count=await s.db.broadcast_confirm(bid,update.effective_user.id,ok)
    await q.answer()
    if count is None:
        await q.edit_message_text('Draft expired, was already handled, or belongs to another staff member.')
    elif not ok:
        await q.edit_message_text(f'Broadcast #{bid} cancelled.')
    else:
        s.delivery_wakeup.set()
        await q.edit_message_text(f'✅ Broadcast #{bid} queued for {count} customers. Check /bstatus for delivery queue stats.')


async def error_handler(update,context):
    log.error('Unhandled Telegram update error: %s',context.error,exc_info=context.error)


async def post_init(app):
    s=app.bot_data['state']
    s.roles=await s.db.roles()
    public=[BotCommand('start','Start customer support'),
            BotCommand('help','Show commands'),BotCommand('games','Choose a game'),
            BotCommand('support','Reach staff'),BotCommand('privacy','Privacy policy'),
            BotCommand('delete_data','Delete customer records'),
            ]
    await app.bot.set_my_commands(public)
    commands=[BotCommand('profile','Customer profile'),BotCommand('id','Customer profile'),
              BotCommand('setaccount','Save game account'),BotCommand('delaccount','Remove game account'),
              BotCommand('findaccount','Find saved username'),BotCommand('accounts','View usernames and passwords'),
              BotCommand('label','Add any custom label'),
              BotCommand('unlabel','Remove a label'),BotCommand('note','Add staff note'),
              BotCommand('history','Customer audit log'),BotCommand('status','Change workflow status'),
              BotCommand('assign','Assign employee'),BotCommand('use','Send saved reply'),
              BotCommand('shortcuts','List saved replies'),BotCommand('close','Close topic'),
              BotCommand('stats','Statistics'),BotCommand('staff','Staff list'),
              BotCommand('block','Block customer'),BotCommand('unblock','Unblock customer'),
              BotCommand('deposit','Staff: record a deposit'),BotCommand('broadcast','Staff: customer broadcast'),
              BotCommand('staffcheck','Check your staff access'),
              BotCommand('bstatus','Broadcast delivery status'),
              BotCommand('bcheck','Check player broadcast'),
              BotCommand('health','Staff: queue health'),BotCommand('addstaff','Staff: add staff'),
              BotCommand('removestaff','Staff: remove staff'),
              BotCommand('addshortcut','Staff: create reply'),BotCommand('delshortcut','Staff: remove reply')]
    try:
        await app.bot.set_my_commands(commands,scope=BotCommandScopeChat(chat_id=s.cfg.group_id))
    except TelegramError as exc:
        log.warning('Could not publish group command menu: %s',exc)
    s.tasks.append(asyncio.create_task(outbox_worker(app,s),name='persistent-outbox'))
    if s.cfg.retention_days:
        s.tasks.append(asyncio.create_task(retention_worker(app,s),name='retention'))
    log.info('%s bot initialized; active staff=%d',BRAND,len(s.roles))


async def post_shutdown(app):
    s=app.bot_data['state']
    for task in s.tasks:task.cancel()
    if s.tasks: await asyncio.gather(*s.tasks,return_exceptions=True)
    await asyncio.to_thread(s.db.close)


def build_app(cfg=None,db=None):
    cfg=cfg or Config()
    cfg.validate()
    db=db or Database(cfg.db_url)
    db.migrate()
    db.seed(cfg.owner_ids,cfg.seed_staff)
    app=(Application.builder().token(cfg.token).post_init(post_init)
         .post_shutdown(post_shutdown).concurrent_updates(False).build())
    app.bot_data['state']=State(cfg,db)
    private=filters.ChatType.PRIVATE
    group=filters.Chat(chat_id=cfg.group_id)
    for command,handler in (
        ('start',customer_start),('help',help_command),('games',game_list),
        ('support',support),('privacy',privacy),('delete_data',delete_data),
        ('optout',optout)):
        app.add_handler(CommandHandler(command,handler,filters=private))
    app.add_handler(CallbackQueryHandler(customer_choice,pattern=r'^(game|bonus):'))
    app.add_handler(CallbackQueryHandler(delete_callback,pattern=r'^delete:(yes|no):\d+$'))
    app.add_handler(CallbackQueryHandler(broadcast_callback,pattern=r'^bc:(yes|no):\d+$'))
    staff_commands={
        'profile':show_profile,'id':show_profile,'accounts':show_accounts,'setaccount':cmd_setaccount,
        'delaccount':cmd_delaccount,'findaccount':find_account,'label':cmd_label,
        'unlabel':cmd_unlabel,'note':cmd_note,'history':show_history,
        'status':cmd_status,'assign':cmd_assign,'deposit':cmd_deposit,
        'close':close_topic,'stats':stats,'health':health,
        'staff':staff_list,'addstaff':addstaff,'removestaff':removestaff,
        'block':block_customer,'unblock':unblock_customer,
        'shortcuts':shortcut_list,'addshortcut':addshortcut,
        'delshortcut':delshortcut,'use':use_shortcut,'broadcast':broadcast,
        'bstatus':broadcast_status,'bcheck':broadcast_check,
        'staffcheck':staffcheck,
    }
    for command,handler in staff_commands.items():
        app.add_handler(CommandHandler(command,handler,filters=group))
    app.add_handler(MessageHandler(private & ~filters.COMMAND,customer_message))
    app.add_handler(MessageHandler(group & ~filters.COMMAND,group_message))
    app.add_error_handler(error_handler)
    return app


def main():
    app=build_app()
    log.info('%s starting polling...',BRAND)
    app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=False)


if __name__=='__main__':
    main()
