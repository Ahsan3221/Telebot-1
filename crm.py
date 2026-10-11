"""Pure formatting, case-insensitive matching and staff account-template parsing."""
import html
import re
from dataclasses import dataclass
from settings import GAMES, LABELS, TRAFFIC_SOURCES, WORKFLOW

# Account suffixes used by staff; any positive number of consecutive '3's.
SUFFIX_GAMES = {
    's': 'Orionstars', 'fk': 'Firekirin', 'v': 'Vblink', 'u': 'Ultrapanda',
    'r': 'Riversweeps', 'j': 'Juwa', 'gv': 'GameVault', 'mw': 'Milkyway',
    'cm': 'CashMaster',
}
GAME_ALIASES = {
    'orion stars': 'Orionstars', 'fire kirin': 'Firekirin',
    'ultra panda': 'Ultrapanda', 'river sweeps': 'Riversweeps',
    'game vault': 'GameVault', 'milky way': 'Milkyway',
    'cash master': 'CashMaster',
}


def short(value, limit=120):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _clean_custom(value, label, limit):
    value = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not value or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f'{label} must contain 1-{limit} printable characters')
    return value


def parse_game(args):
    if not args:
        return None
    key = short(args[0], 70).casefold()
    return next((g for g in GAMES if g.casefold() == key), GAME_ALIASES.get(key))


def parse_source(args):
    if not args:
        return None
    source = args[0].strip().casefold().removeprefix('src_')
    return TRAFFIC_SOURCES.get(source)


def normalize_label(value):
    """Identity key for case-insensitive labels; no predefined label restriction."""
    return _clean_custom(value, 'Label', 60).casefold()


def label_display(value, display=None):
    key = normalize_label(value)
    return LABELS.get(key, _clean_custom(display or value, 'Label', 60))


def normalize_game(value):
    v = _clean_custom(value, 'Game name', 60)
    return parse_game([v]) or v


def normalize_workflow(value):
    # Known statuses use snake_case; unknown staff-entered statuses are also allowed.
    v = _clean_custom(value, 'Workflow status', 60).casefold().replace('-', '_').replace(' ', '_')
    return v


def safe_username(value):
    """Allow user-defined usernames; preserve casing, normalize whitespace."""
    name = _clean_custom(value, 'Account username', 80).lstrip('@')
    if not name:
        raise ValueError('Account username cannot be blank')
    return name


def infer_game(username):
    name = safe_username(username)
    match = re.search(r'(?i)3+(fk|gv|mw|cm|s|v|u|r|j)$', name)
    if not match or match.start() == 0:
        return None
    return SUFFIX_GAMES[match.group(1).casefold()]


@dataclass(frozen=True)
class CapturedAccount:
    game: str
    username: str
    password: str


# Require both credential lines to avoid mistaking ordinary messages for accounts.
_USER_LINE = re.compile(r'^\s*(?:[^\w\n]{0,8}\s*)?(?:user\s*name|username|login\s*id)\s*[:：]\s*(.+?)\s*$', re.I | re.M)
_PASS_LINE = re.compile(r'^\s*(?:[^\w\n]{0,8}\s*)?(?:pass\s*word|password|passcode)\s*[:：]\s*(.*?)\s*$', re.I | re.M)
_GAME_LINE = re.compile(r'^\s*(?:[^\w\n]{0,8}\s*)?(?:game|platform)\s*[:：]\s*(.+?)\s*$', re.I | re.M)


def parse_account_template(text):
    """Parse outbound staff template only. Never parse free-form customer messages."""
    text = str(text or '')
    if len(text) > 4096:
        return None
    users, passes = _USER_LINE.findall(text), _PASS_LINE.findall(text)
    if len(users) != 1 or len(passes) != 1:
        return None
    try:
        username = safe_username(users[0])
        password = passes[0].strip()
        if not password or len(password) > 256 or any(ord(c) < 32 for c in password):
            return None
        # Explicit Game: overrides inference for custom/non-suffixed account names.
        games = _GAME_LINE.findall(text)
        game = normalize_game(games[0]) if len(games) == 1 else infer_game(username)
        if not game:
            return None
        return CapturedAccount(game, username, password)
    except ValueError:
        return None


def topic_title(row):
    key = row.get('primary_label') or 'new'
    label = row.get('primary_label_display') or LABELS.get(key, key)
    name = short(row.get('name') or row.get('username') or str(row['user_id']), 48)
    game = short(row.get('game'), 30)
    return (f'{label} | {name}' + (f' | {game}' if game else ''))[:120]


def profile_text(row, accounts=(), labels=(), notes=(), deposits=()):
    handle = f"@{row['username']}" if row.get('username') else '(no Telegram username)'
    def tag(x):
        if isinstance(x, dict):
            return label_display(x['label'], x.get('label_display'))
        return label_display(x)
    tags = ', '.join(tag(x) for x in labels) or '🆕 New'
    acct = '\n'.join(f"  • {x['game']}: {short(x['account_username'], 80)}" for x in accounts) or '  (none saved)'
    note_lines = '\n'.join(f"  • {short(x['body'], 200)}" for x in notes[:3]) or '  (none)'
    amount = float(deposits['amount']) if deposits else 0.0
    return (
        '📌 CUSTOMER PROFILE — WISHWHEEL\n'
        '━━━━━━━━━━━━━━━━━━\n'
        f"👤 {short(row.get('name'), 90)}\n"
        f'🔗 {handle}\n🆔 {row["user_id"]}\n'
        f'🏷 {tags}\n'
        f"🎮 Selected: {row.get('game') or 'None'}\n"
        f"📋 Status: {row.get('workflow_status') or 'new'}\n"
        f"🚫 Support access: {'BLOCKED' if row.get('is_blocked') else 'ACTIVE'}\n"
        f"👨‍💼 Assigned staff ID: {row.get('assigned_staff') or 'Unassigned'}\n"
        '━━━━━━━━━━━━━━━━━━\n'
        f'🎮 SAVED GAME ACCOUNTS\n{acct}\n'
        '━━━━━━━━━━━━━━━━━━\n'
        f'📝 LATEST NOTES\n{note_lines}\n'
        '━━━━━━━━━━━━━━━━━━\n'
        f"💳 Recorded deposits: {deposits['count'] if deposits else 0} | ${amount:.2f}\n"
        f"🎁 Bonus: {row.get('bonus') or 'None'}\n"
        f"📊 Source: {row.get('source') or 'Direct'}\n"
        'ℹ️ Use /profile, /accounts, /setaccount, /label, /note.\n'
        '⚠️ Amounts are staff-entered records, not payment-provider verification.'
    )[:3950]


def escape_text(value):
    return html.escape(str(value), quote=False)


def retry_delay(attempt):
    return min(3600, 2 ** min(int(attempt), 10))


def is_permanent_error(error):
    msg = str(error).casefold()
    return any(s in msg for s in (
        'bot was blocked', 'user is deactivated', 'chat not found', 'user not found',
        'message to copy not found', "message can't be copied", 'message can not be copied',
        'not enough rights', 'bot is not a member', 'forbidden', 'message_id_invalid',
    ))


def is_topic_missing(error):
    msg = str(error).casefold()
    return any(s in msg for s in ('message thread not found', 'thread_not_found', 'forum topic not found', 'topic_closed', 'topic is closed'))
