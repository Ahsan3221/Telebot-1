"""Broadcast formatting regression: typed message + first name, no brand header.

The customer payload and the staff topic mirror must both be clean, including
jobs queued before the deployment of this change.
"""
import asyncio
import ast
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from settings import BRAND

ROOT=Path(__file__).resolve().parents[1]
MODULE = "crm_runtime" if (ROOT/"crm_runtime.py").exists() else "bot"
SOURCE=(ROOT/(MODULE+".py")).read_text(encoding="utf-8")
DB=(ROOT/"database.py").read_text(encoding="utf-8")
SAMPLE="Hi Rachel 🙈 50% Deposit Bonus Activate | 🥳 Message Now and Claim!"


def _runtime():
    return importlib.import_module(MODULE)


def _old_headers():
    headers=[f"📢 {BRAND} UPDATE\n\n"]
    if BRAND=="Winning Wave":
        headers.append("📢 Ace Arcade UPDATE\n\n")
    return headers


def test_broadcast_creation_preserves_staff_text():
    ast.parse(SOURCE)
    ast.parse(DB)
    assert "content=row['body'].replace('{name}', first)" in DB
    assert "content=f\"📢 " not in DB
    assert "first=(user['name'] or 'there').split(' ')[0]" in DB


def test_no_premade_title_for_new_or_old_queued_broadcasts():
    runtime=_runtime()
    assert runtime.clean_broadcast_body(SAMPLE)==SAMPLE
    for old_prefix in _old_headers():
        assert runtime.clean_broadcast_body(old_prefix+SAMPLE)==SAMPLE
    # Text typed by staff with a similar phrase in the middle is preserved.
    assert runtime.clean_broadcast_body("Hello 📢 "+BRAND+" UPDATE\n\nOffer")==(
        "Hello 📢 "+BRAND+" UPDATE\n\nOffer")


def test_pending_legacy_broadcast_sends_only_customer_message():
    runtime=_runtime()
    async def exercise():
        db=SimpleNamespace(user=AsyncMock(return_value={
            'is_blocked':False,'bot_blocked':False,'marketing_opted_out':False}))
        api=SimpleNamespace(send_message=AsyncMock())
        s=SimpleNamespace(db=db,cfg=SimpleNamespace(group_id=-1003))
        for prefix in _old_headers():
            await runtime.deliver_job(SimpleNamespace(bot=api),s,{
                'user_id':987,'direction':'broadcast','body':prefix+SAMPLE})
        assert api.send_message.await_count==len(_old_headers())
        for item in api.send_message.await_args_list:
            assert item.kwargs=={'chat_id':987,'text':SAMPLE}
    asyncio.run(exercise())


def test_staff_topic_keeps_only_broadcast_sent_marker():
    runtime=_runtime()
    async def exercise():
        db=SimpleNamespace(outbox_claim_mirror=AsyncMock(return_value={
            'id':31,'attempts':1,'topic_id':234,'topic_status':'open',
            'body':_old_headers()[0]+SAMPLE}),
            outbox_mirror_result=AsyncMock())
        api=SimpleNamespace(send_message=AsyncMock())
        s=SimpleNamespace(db=db,cfg=SimpleNamespace(group_id=-1003,
                                                     max_delivery_attempts=8))
        ok=await runtime.mirror_one_broadcast(SimpleNamespace(bot=api),s)
        assert ok is True
        api.send_message.assert_awaited_once_with(
            chat_id=-1003,message_thread_id=234,
            text='📢 BROADCAST SENT\n\n'+SAMPLE)
        db.outbox_mirror_result.assert_awaited_once_with(31,True)
    asyncio.run(exercise())


def test_name_replacement_is_only_personalization():
    msg='Hi {name} 🙈 50% Deposit Bonus Activate'
    assert msg.replace('{name}','Rachel')=='Hi Rachel 🙈 50% Deposit Bonus Activate'
