"""Configuration and validation for the WishWheel support bot."""
import os
import logging
import re

BRAND = "WishWheel"
PREFIX = "wishwheel"
GAMES = (
    "Orionstars", "Firekirin", "Ultrapanda", "Juwa", "GameVault",
    "Riversweeps", "Milkyway", "Vblink", "Gameroom", "CashMaster",
)
TRAFFIC_SOURCES = {"website": "🌐 Website", "facebook": "📘 Facebook"}
BONUS_OPTIONS = {
    "signup120": "💰 120% Signup Bonus",
    "freeplay": "🎁 Redeemable Freeplay",
}
LABELS = {"new": "🆕 New", "freeplay": "🎁 Freeplay", "depositor": "💰 Depositor",
          "regular": "⭐ Regular", "vip": "👑 VIP"}
WORKFLOW = {"new", "pending", "in_progress", "resolved"}


def env_ids(name):
    """Read ID lists separated by commas, spaces, semicolons or JSON brackets."""
    raw = os.getenv(name, "")
    return {int(item) for item in re.findall(r"(?<![-\w])\d+(?!\w)", raw)
            if int(item) > 0}


# ID-bearing infrastructure fields are never treated as staff accounts.
_NON_STAFF_ID_PARTS = (
    "GROUP", "CHAT", "CHANNEL", "BOT", "APP", "DATABASE", "DB_", "ACCOUNT",
    "BUSINESS", "PROJECT", "TOPIC", "MESSAGE", "CAMPAIGN", "PAYMENT",
    "SERVICE", "NETWORK", "CUSTOMER", "PLAYER", "WEBHOOK", "RAILWAY",
    "PORTFOLIO", "AD_", "TRANSACTION", "ORDER",
)


def configured_staff_sources():
    """Honor named Telegram staff ID variables, including individually named team IDs.

    For example: AUTHORIZED_STAFF_IDS, STAFF_IDS, ADMIN_IDS, MANAGER_IDS,
    SALMAN_ID, RAMZAN_ID. This never interprets token or infrastructure fields.
    """
    sources = {}
    for key in os.environ:
        key_upper = key.upper()
        if not key_upper.endswith(("_ID", "_IDS")):
            continue
        if any(part in key_upper for part in _NON_STAFF_ID_PARTS):
            continue
        for user_id in env_ids(key):
            sources.setdefault(user_id, set()).add(key)
    return sources


def positive_int(name, default):
    value = int(os.getenv(name, default))
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


class Config:
    def __init__(self):
        self.token = os.getenv("BOT_TOKEN", "").strip()
        self.group_id = int(os.getenv("SUPPORT_GROUP_ID", "0") or "0")
        self.db_url = os.getenv("DATABASE_URL", "").strip()
        self.staff_id_sources = configured_staff_sources()
        self.owner_ids = env_ids("OWNER_IDS") | env_ids("OWNER_ID")
        self.seed_staff = set(self.staff_id_sources) - self.owner_ids
        self.privacy_url = os.getenv("PRIVACY_POLICY_URL", "").strip()
        self.retention_days = positive_int("RETENTION_DAYS", "0")
        self.broadcast_gap = max(1.0, float(os.getenv("BROADCAST_GAP_SECONDS", "1.0")))
        self.max_delivery_attempts = max(1, positive_int("MAX_DELIVERY_ATTEMPTS", "8"))

    def validate(self):
        if not self.token:
            raise RuntimeError("BOT_TOKEN is missing")
        if not self.group_id:
            raise RuntimeError("SUPPORT_GROUP_ID is missing")
        if not self.db_url:
            raise RuntimeError("DATABASE_URL is missing")
        # Legacy apps may have no OWNER_IDS yet. Avoid crashing on upgrade.
