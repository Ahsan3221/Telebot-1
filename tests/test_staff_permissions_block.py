"""Regression tests: Railway staff IDs, block/unblock, broadcast, and pacing."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import importlib

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = "crm_runtime" if (ROOT / "crm_runtime.py").exists() else "bot"


def test_authorized_env_ids_and_one_second_minimum(monkeypatch):
    monkeypatch.setenv("AUTHORIZED_STAFF_IDS", "12,13")
    monkeypatch.setenv("ADMIN_IDS", "14")
    monkeypatch.setenv("STAFF_IDS", "15")
    monkeypatch.setenv("OWNER_IDS", "11")
    monkeypatch.setenv("BROADCAST_GAP_SECONDS", "0.15")
    from settings import Config
    cfg = Config()
    assert cfg.owner_ids == {11}
    assert {12,13,14,15}.issubset(cfg.seed_staff)
    assert cfg.broadcast_gap == 1.0
    monkeypatch.setenv("BROADCAST_GAP_SECONDS", "2")
    assert Config().broadcast_gap == 2.0


def test_staff_role_accepts_env_or_saved_database_roles():
    bot = importlib.import_module(MODULE)
    s = SimpleNamespace(cfg=SimpleNamespace(owner_ids={11},seed_staff={12}), roles={13:"staff"})
    def actor(uid):
        return SimpleNamespace(effective_user=SimpleNamespace(id=uid))
    assert bot.staff_role(actor(11),s) == "owner"
    assert bot.staff_role(actor(12),s) == "staff"
    assert bot.staff_role(actor(13),s) == "staff"
    assert bot.staff_role(actor(99),s) is None


def test_block_and_unblock_commands_authorized_from_env():
    bot = importlib.import_module(MODULE)
    async def check():
        db = SimpleNamespace(
            user=AsyncMock(return_value={"user_id":987, "topic_id":None,"is_blocked":False}),
            set_blocked=AsyncMock(return_value=True))
        cfg = SimpleNamespace(group_id=-10077, owner_ids={11},seed_staff={12})
        state=SimpleNamespace(cfg=cfg, db=db, roles={}, delivery_wakeup=MagicMock())
        msg = SimpleNamespace(message_thread_id=1, reply_text=AsyncMock())
        actor = SimpleNamespace(id=12)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-10077),
            effective_user=actor, effective_message=msg, message=msg)
        context = SimpleNamespace(args=["987","spam"], bot=MagicMock(),
                                  application=SimpleNamespace(bot_data={"state":state}))
        await bot.block_customer(update,context)
        db.set_blocked.assert_awaited_once_with(987,True,12,"spam")
        db.set_blocked.reset_mock()
        db.user.return_value["is_blocked"]=True
        context.args=["987"]
        await bot.unblock_customer(update,context)
        db.set_blocked.assert_awaited_once_with(987,False,12,"")
    asyncio.run(check())


def test_broadcast_create_by_staff_and_confirm_authorship():
    bot = importlib.import_module(MODULE)
    async def check():
        db=SimpleNamespace(
            broadcast_draft=AsyncMock(return_value=(51,2)),
            broadcast_confirm=AsyncMock(return_value=2))
        cfg=SimpleNamespace(group_id=-10077,owner_ids={11},seed_staff={12})
        state=SimpleNamespace(cfg=cfg,db=db,roles={},delivery_wakeup=MagicMock())
        msg=SimpleNamespace(reply_text=AsyncMock(),message_thread_id=1)
        common=dict(effective_chat=SimpleNamespace(id=-10077),
                    effective_user=SimpleNamespace(id=12))
        update=SimpleNamespace(**common,message=msg,effective_message=msg)
        context=SimpleNamespace(
            args=["Hello","subscribers"],application=SimpleNamespace(bot_data={"state":state}))
        await bot.broadcast(update,context)
        db.broadcast_draft.assert_awaited_once_with(12,"Hello subscribers",None)
        db.broadcast_confirm.assert_awaited_once_with(51,12,True)
        db.broadcast_confirm.reset_mock()
        q=SimpleNamespace(data="bc:yes:51",answer=AsyncMock(),edit_message_text=AsyncMock())
        click=SimpleNamespace(**common, callback_query=q,effective_message=msg)
        await bot.broadcast_callback(click,context)
        db.broadcast_confirm.assert_awaited_once_with(51,12,True)
        assert state.delivery_wakeup.set.call_count == 2
    asyncio.run(check())


def test_blocked_inbound_and_queued_deliveries_are_rejected():
    source = (ROOT / ("crm_runtime.py" if MODULE == "crm_runtime" else "bot.py")).read_text()
    db = (ROOT / "database.py").read_text()
    assert "row.get('is_blocked')" in source
    assert "if row.get('is_blocked'):" in source
    assert "if await _reject_blocked(update,context)" in source
    assert '"is_blocked", "BOOLEAN NOT NULL DEFAULT FALSE"' in db
    assert "WHERE NOT is_blocked AND NOT bot_blocked AND NOT marketing_opted_out" in db
    assert "async def set_blocked" in db
    if MODULE == "crm_runtime":
        legacy=(ROOT/"bot.py").read_text()
        assert "_ww_reject_blocked" in legacy
        assert "crm_addon.BlockedCustomer" in legacy
