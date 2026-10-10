# WishWheel Telegram Support Bot — Railway PostgreSQL CRM

**This repository is the Railway/PostgreSQL WishWheel bot (Telebot-1), not the old local SQLite Telebot repository.**

## Features

- Preserves existing customer forum topics and PostgreSQL tables `users`, `topics`, `staff`
- Case-insensitive custom labels and custom game names; game selection includes CashMaster
- Automatic game username/password capture from authorized staff's normal message templates
- Recognizes `3s`, `33s`, `333s`, `3fk`, `3u`, `3v`, `3j`, `3r`, `3gv`, `3mw`, `3cm` and repeats of 3
- Pinned customer profile, `/accounts`, `/setaccount`, `/findaccount`, `/label`, `/unlabel`, `/note`, `/history`
- Role-based permissions, opted-in broadcasts, persistent delivery queue, retries and privacy requests

## Environment for Railway

Keep existing environment variables:

```dotenv
BOT_TOKEN=<your existing WishWheel bot token>
SUPPORT_GROUP_ID=<your existing Telegram support supergroup ID>
DATABASE_URL=<your existing WishWheel PostgreSQL URL>
AUTHORIZED_STAFF_IDS=<optional comma-separated staff IDs>
OWNER_IDS=<your numeric Telegram user ID>
```

**No SQLite dependency.** `Procfile` runs `python bot.py`. Existing PostgreSQL records stay under `users`, `topics`, and `staff`. New CRM tables start with `wishwheel_`; no PostgreSQL database is recreated.

## Account capture

Staff sends the normal account message inside the customer's topic:

```text
👤 Username: Michael333fk
🔑 Password: 009988
Log in and let me know if you need help ❤️✨
```

The bot recognizes Firekirin and saves the exact username and password to the customer's PostgreSQL record. `/accounts` displays saved credentials to authorized staff.

**Game passwords are stored in plaintext** by request. Restrict PostgreSQL access and never commit database credentials to GitHub.

## Backups and safety

Original source, legacy `support.db`, `support.db-wal`, `support.db-shm`, `support_data.json`, old Windows scripts and `Procfile.txt` were retained in the GitHub branch `backup/pre-postgres-crm-20261011`. They were removed from `main`, but they remain in Git history and the backup branch.

A GitHub source backup is **not** a Railway PostgreSQL backup. Take a PostgreSQL snapshot before deploying.

Offline tests: `python -m pytest -q`. Live Railway/PostgreSQL/Telegram integration tests are still required.
