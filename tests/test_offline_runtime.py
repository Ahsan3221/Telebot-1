"""Offline tests for runtime logic without calling Telegram/PostgreSQL APIs."""
import asyncio
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_bot_stubbed(monkeypatch):
    tg=types.ModuleType('telegram')
    ex=types.ModuleType('telegram.ext')
    errors=types.ModuleType('telegram.error')
    psy=types.ModuleType('psycopg2')
    psy_pool=types.ModuleType('psycopg2.pool')
    psy_extras=types.ModuleType('psycopg2.extras')
    for name in ('BotCommand','BotCommandScopeChat','InlineKeyboardButton','InlineKeyboardMarkup','Update'):
        setattr(tg,name,type(name,(),{'ALL_TYPES':[]}) if name=='Update' else MagicMock())
    for name in ('Application','CallbackQueryHandler','CommandHandler','ContextTypes','MessageHandler','filters'):
        setattr(ex,name,MagicMock())
    class TelegramError(Exception):pass
    class RetryAfter(TelegramError):
        def __init__(self,delay):
            super().__init__('Rate limit')
            self.retry_after=delay
    errors.TelegramError=TelegramError
    errors.RetryAfter=RetryAfter
    psy.IntegrityError=type('IntegrityError',(Exception,),{})
    psy_pool.ThreadedConnectionPool=MagicMock()
    psy_extras.RealDictCursor=MagicMock()
    monkeypatch.setitem(sys.modules,'telegram',tg)
    monkeypatch.setitem(sys.modules,'telegram.ext',ex)
    monkeypatch.setitem(sys.modules,'telegram.error',errors)
    monkeypatch.setitem(sys.modules,'psycopg2',psy)
    monkeypatch.setitem(sys.modules,'psycopg2.pool',psy_pool)
    monkeypatch.setitem(sys.modules,'psycopg2.extras',psy_extras)
    sys.modules.pop('bot',None)
    sys.modules.pop('database',None)
    return importlib.import_module('bot')


def test_per_user_lock_race(monkeypatch):
    bot=load_bot_stubbed(monkeypatch)
    config=types.SimpleNamespace()
    s=bot.State(config,object())
    observed=[]
    active=[0]
    async def run():
        async def one(i):
            async with s.lock(1):
                active[0]+=1
                observed.append(active[0])
                await asyncio.sleep(0.001)
                active[0]-=1
        await asyncio.gather(*(one(i) for i in range(15)))
        assert 1 not in s._user_locks
    asyncio.run(run())
    assert set(observed)=={1}


def test_outbox_customer_copy(monkeypatch):
    bot=load_bot_stubbed(monkeypatch)
    db=types.SimpleNamespace(user=AsyncMock(return_value={'topic_id':918,'marketing_opt_in':False}))
    api=types.SimpleNamespace(copy_message=AsyncMock())
    app=types.SimpleNamespace(bot=api)
    cfg=types.SimpleNamespace(group_id=-100999)
    s=types.SimpleNamespace(db=db,cfg=cfg)
    job={'user_id':77,'direction':'customer','source_chat_id':77,'source_message_id':88}
    asyncio.run(bot.deliver_job(app,s,job))
    api.copy_message.assert_awaited_once_with(chat_id=-100999,message_thread_id=918,
                                               from_chat_id=77,message_id=88)


def test_optout_prevents_queued_broadcast(monkeypatch):
    bot=load_bot_stubbed(monkeypatch)
    db=types.SimpleNamespace(user=AsyncMock(return_value={'topic_id':918,'marketing_opt_in':False}))
    api=types.SimpleNamespace(send_message=AsyncMock())
    s=types.SimpleNamespace(db=db,cfg=types.SimpleNamespace(group_id=123))
    result=asyncio.run(bot.deliver_job(types.SimpleNamespace(bot=api),s,
                                        {'user_id':77,'direction':'broadcast','body':'Promo'}))
    assert result is True
    api.send_message.assert_not_awaited()
