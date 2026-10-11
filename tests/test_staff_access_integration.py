"""Regression for Railway staff IDs, live DB roles, and Telegram admins.

Unlike source-string-only checks these tests exercise the real authorization
entry points with simulated Telegram update and bot responses.
"""
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

MOD = "crm_runtime" if __import__("pathlib").Path(__file__).resolve().parents[1].joinpath("crm_runtime.py").exists() else "bot"


def run(coro):
    return asyncio.run(coro)


def mocked_group(user_id, *, db_roles=None, env_ids=(), telegram_status="member", sender_chat=None):
    module = importlib.import_module(MOD)
    config = SimpleNamespace(group_id=-1005555, owner_ids=set(),
                             seed_staff=set(env_ids), staff_id_sources={})
    state = SimpleNamespace(cfg=config, roles={}, db=SimpleNamespace(
        roles=AsyncMock(return_value=db_roles or {})))
    message = SimpleNamespace(reply_text=AsyncMock(),
                              sender_chat=sender_chat, message_thread_id=1)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=-1005555),
        effective_message=message, message=message)
    bot = SimpleNamespace(get_chat_member=AsyncMock(
        return_value=SimpleNamespace(status=telegram_status)))
    ctx = SimpleNamespace(bot=bot, application=SimpleNamespace(bot_data={"state":state}))
    return module,update,ctx,state,message


def test_id_parser_handles_railway_formats_and_ignores_service_ids(monkeypatch):
    from settings import Config, env_ids
    monkeypatch.setenv("AUTHORIZED_STAFF_IDS", "[1234, 2345; 3456]")
    monkeypatch.setenv("SALMAN_ID", "4567")
    monkeypatch.setenv("MOHEET_ID", "5678")
    monkeypatch.setenv("OWNER_IDS", "6789")
    monkeypatch.setenv("SUPPORT_GROUP_ID", "-100123456")
    monkeypatch.setenv("APP_ID", "7788")
    monkeypatch.setenv("CAMPAIGN_ID", "8899")
    assert env_ids("AUTHORIZED_STAFF_IDS")=={1234,2345,3456}
    cfg=Config()
    assert {1234,2345,3456,4567,5678}.issubset(cfg.seed_staff)
    assert 6789 in cfg.owner_ids
    assert 7788 not in cfg.seed_staff and 8899 not in cfg.seed_staff
    assert 100123456 not in cfg.seed_staff
    assert "SALMAN_ID" in cfg.staff_id_sources[4567]


def test_env_member_can_use_staff_commands():
    module,update,ctx,state,msg=mocked_group(4567,env_ids={4567})
    assert run(module.authorize(update,ctx))
    state.db.roles.assert_not_awaited()
    ctx.bot.get_chat_member.assert_not_awaited()


def test_db_staff_added_after_startup_is_refreshed():
    module,update,ctx,state,msg=mocked_group(4567,db_roles={4567:"support_agent"})
    assert run(module.authorize(update,ctx))
    assert state.roles=={4567:"support_agent"}
    state.db.roles.assert_awaited_once()
    ctx.bot.get_chat_member.assert_not_awaited()


@pytest.mark.parametrize("status", ["administrator", "creator"])
def test_telegram_group_admin_can_use_commands(status):
    module,update,ctx,state,msg=mocked_group(4567,telegram_status=status)
    assert run(module.authorize(update,ctx))
    ctx.bot.get_chat_member.assert_awaited_once_with(
        chat_id=-1005555,user_id=4567)


def test_regular_unlisted_group_member_is_denied_with_useful_diagnostics():
    module,update,ctx,state,msg=mocked_group(4567)
    assert not run(module.authorize(update,ctx))
    msg.reply_text.assert_awaited_once()
    assert "/staffcheck" in msg.reply_text.await_args.args[0]
    assert "4567" in msg.reply_text.await_args.args[0]


def test_anonymous_admin_cannot_impersonate_a_human():
    module,update,ctx,state,msg=mocked_group(
        1087968824,sender_chat=SimpleNamespace(id=-1005555))
    assert not run(module.authorize(update,ctx))
    assert "Anonymous admin" in msg.reply_text.await_args.args[0]


def test_staffcheck_is_available_to_unrecognized_staff_without_granting_access():
    module,update,ctx,state,msg=mocked_group(4567)
    run(module.staffcheck(update,ctx))
    output=msg.reply_text.await_args.args[0]
    assert "4567" in output
    assert "NOT AUTHORIZED" in output


def test_saved_staff_consistently_authorized_in_forum_flow():
    module,update,ctx,state,msg=mocked_group(4567,db_roles={4567:"staff"})
    assert run(module.staff_access(update,ctx))=="staff"
    assert run(module.authorize(update,ctx))
