"""Broadcast regression checks: no default opt-in filter, all-registered delivery,
status diagnostics, and separate durable staff-topic mirrors."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import importlib

ROOT=Path(__file__).resolve().parents[1]
MODULE="crm_runtime" if (ROOT/"crm_runtime.py").exists() else "bot"
BOT=ROOT/(MODULE+".py")
DB=(ROOT/"database.py").read_text(encoding="utf-8")
SRC=BOT.read_text(encoding="utf-8")


def test_valid_python_and_commands():
    ast.parse(SRC)
    ast.parse(DB)
    assert "async def broadcast_status(" in SRC
    assert "async def broadcast_check(" in SRC
    assert "'bstatus':broadcast_status" in SRC
    assert "'bcheck':broadcast_check" in SRC


def test_db_targets_registered_nonblocked_users_and_sorts_oldest_first():
    assert "WHERE marketing_opt_in AND NOT is_blocked" not in DB
    assert DB.count("WHERE NOT is_blocked AND NOT bot_blocked AND NOT marketing_opted_out") == 4
    assert "ORDER BY last_seen ASC,topic_id ASC,user_id ASC" in DB
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in DB


def test_group_copy_only_follows_successful_private_delivery():
    assert "sent['direction']=='broadcast'" in DB
    assert "INSERT INTO {M}(broadcast_job_id)" in DB
    assert "mirror_one_broadcast" in SRC
    assert "await s.db.outbox_result(job['id'],True)" in SRC
    assert "await mirror_one_broadcast(application,s)" in SRC
    assert "message_thread_id=int(job['topic_id'])" in SRC
    assert "async def broadcast_report(" in DB
    assert "async def broadcast_check(" in DB


def test_unreachable_detection():
    runtime=importlib.import_module(MODULE)
    assert runtime.unreachable_player(Exception("Forbidden: bot was blocked by the user"))
    assert runtime.unreachable_player(Exception("Bad Request: chat not found"))
    assert not runtime.unreachable_player(Exception("Too Many Requests: retry after 4"))


def test_mirror_sends_only_to_staff_chat():
    runtime=importlib.import_module(MODULE)
    async def case():
        row={'id':44,'attempts':0,'topic_id':999,'topic_status':'open','body':'Hello'}
        db=SimpleNamespace(outbox_claim_mirror=AsyncMock(return_value=row),
                           outbox_mirror_result=AsyncMock())
        bot=SimpleNamespace(send_message=AsyncMock())
        state=SimpleNamespace(db=db,cfg=SimpleNamespace(group_id=-100909,
                                                         max_delivery_attempts=4))
        assert await runtime.mirror_one_broadcast(SimpleNamespace(bot=bot),state) is True
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs['chat_id']==-100909
        assert bot.send_message.await_args.kwargs['message_thread_id']==999
        db.outbox_mirror_result.assert_awaited_once_with(44,True)
    asyncio.run(case())


def test_draft_command_queues_and_wakes_worker():
    runtime=importlib.import_module(MODULE)
    async def case():
        db=SimpleNamespace(broadcast_draft=AsyncMock(return_value=(22,127)),
                           broadcast_confirm=AsyncMock(return_value=127))
        state=SimpleNamespace(db=db, cfg=SimpleNamespace(owner_ids={12},
             seed_staff=set(),group_id=-10021), roles={}, delivery_wakeup=MagicMock())
        message=SimpleNamespace(reply_text=AsyncMock())
        update=SimpleNamespace(message=message,effective_message=message,
             effective_user=SimpleNamespace(id=12),effective_chat=SimpleNamespace(id=-10021))
        context=SimpleNamespace(args=['Hello','{name}'],
             application=SimpleNamespace(bot_data={'state':state}))
        await runtime.broadcast(update,context)
        db.broadcast_draft.assert_awaited_once_with(12,'Hello {name}',None)
        db.broadcast_confirm.assert_awaited_once_with(22,12,True)
        state.delivery_wakeup.set.assert_called_once()
    asyncio.run(case())
