# Broadcast, Staff Permissions, and Customer Block/Unblock

This update keeps the existing Telegram bot branding, customer records, game accounts, and PostgreSQL schema. It adds an **additive** `is_blocked` boolean to the existing users table.

## Authorized staff

The following Railway environment variables accept comma-separated numeric Telegram user IDs:
- `OWNER_IDS`
- `AUTHORIZED_STAFF_IDS`
- `ADMIN_IDS` (optional alias)
- `STAFF_IDS` (optional alias)

All listed IDs can use customer management and CRM functions, including broadcast, confirm, block/unblock, deposit recording, shortcuts, queue health, and staff administration. Previously added active staff from the database also retain access. Railway-configured IDs cannot be revoked from Telegram; remove the ID from Railway's environment and redeploy if needed. Do not put customer IDs into these variables.

## Broadcast

```text
/broadcast Hello {name}, we have an update.
/broadcast --label=VIP Hello {name}, an update for you.
```

A preview appears; **the same staff member who created the draft** must tap Confirm or Cancel. Only customers who have opted in using `/optin` and who are not blocked will be queued. The bot respects `/optout`, including requests made after a broadcast is queued.

`BROADCAST_GAP_SECONDS=1.0` means at least one second **between broadcast deliveries in this bot worker**; lower old values are automatically clamped to one second. Telegram API rate limits and retries may make actual delivery slower. Already queued messages may be dropped if a customer becomes blocked.

## Block / unblock

Inside a customer support topic:
```text
/block abusive messages
/unblock
```

Anywhere inside the authorized staff group:
```text
/block 123456789 reason
/unblock 123456789
```

Block means bot-level support suspension; it does not ban the Telegram account or block other bots. It prevents new support messages, game-selection/start interactions, and queued delivery to/from the customer, including broadcasts. The blocked flag persists in PostgreSQL and is visible in the pinned customer profile; all changes are audited. Customers can still use `/privacy`, `/optout` and `/delete_data` to exercise data/privacy rights. Unblock restores messaging.

## Deployment

Back up Railway PostgreSQL before updating; GitHub source backups do not contain live database contents. After the commit deploys, check Railway logs for migrations and run `/broadcast`, `/block`, `/unblock` in your private support group. These are offline-tested changes; live Railway/Telegram behavior must be verified after deployment.
