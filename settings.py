"""Configuration and validation for the WishWheel support bot."""
import os
import logging

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
    return {int(part.strip()) for part in os.getenv(name, "").split(",")
            if part.strip().isdigit() and int(part.strip()) > 0}


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
        self.seed_staff = env_ids("AUTHORIZED_STAFF_IDS") | env_ids("ADMIN_IDS") | env_ids("STAFF_IDS")
        self.owner_ids = env_ids("OWNER_IDS")
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
