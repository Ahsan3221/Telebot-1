"""Regression: /start enrolls new players without separate opt-in UI.
Old subscription state remains unchanged; existing opt-outs are not overridden.
"""
import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
LEGACY=ROOT/"bot.py"
CRM=ROOT/("crm_runtime.py" if (ROOT/"crm_runtime.py").exists() else "bot.py")


def test_python_source_is_valid():
    ast.parse(LEGACY.read_text(encoding="utf-8"))
    ast.parse(CRM.read_text(encoding="utf-8"))


def test_no_optin_command_or_user_optout_button():
    source=LEGACY.read_text(encoding="utf-8")
    crm=CRM.read_text(encoding="utf-8")
    for text in (source,crm):
        assert "BotCommand('optin'" not in text
        assert 'BotCommand("optin"' not in text
        assert "BotCommand('optout'" not in text
        assert 'BotCommand("optout"' not in text
        assert "('optin',optin)" not in text
    # Keep a private server-side optout route so prior preferences remain respected.
    assert "('optout',optout)" in crm or '("optout", crm_addon.optout)' in source


def test_first_start_enrolls_only_new_customers():
    source=LEGACY.read_text(encoding="utf-8")
    if CRM==LEGACY:
        assert "if previous is None:" in source
        assert "await s.db.opt_in(user.id,True)" in source
    else:
        assert "was_known_to_crm is None" in source
        assert "await _crm_state.db.opt_in(user.id, True)" in source


def test_broadcast_remains_optout_respecting():
    database=(ROOT/"database.py").read_text(encoding="utf-8")
    assert database.count("WHERE NOT is_blocked AND NOT bot_blocked AND NOT marketing_opted_out") >= 2
    assert "async def opt_in(" in database
    assert "marketing_opted_out=%s" in database


def test_no_extra_customer_welcome_notice():
    source=LEGACY.read_text(encoding="utf-8")
    if CRM==LEGACY:
        start=source[source.index("async def customer_start("):source.index("async def help_command(")]
    else:
        start=source[source.index("async def start("):source.index("async def finalize_start(")]
    # Only inspect user-visible string literals, not developer comments.
    tree = ast.parse(start)
    literals = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    visible = " ".join(literals).casefold()
    assert "optin" not in visible
    assert "optout" not in visible
    assert "subscribed" not in visible
