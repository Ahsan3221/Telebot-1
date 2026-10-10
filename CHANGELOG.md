# Upgrade Notes — Source code audit and CRM redesign

## Critical source issues addressed

- Original README listed `/privacy` and `/delete_data` that `bot.py` did not register. Added with confirmation flow.
- Original owner/admin privileges were not separated; `/addstaff` and `/removestaff` worked for any staff. Added `OWNER_IDS` owner role (with legacy `AUTHORIZED_STAFF_IDS` fallback when unset), explicit authorization.
- Original removal did not survive reboot for users seeded in `AUTHORIZED_STAFF_IDS`. New `active` column preserves explicit revocation.
- Original queue used volatile Python `asyncio.Queue` and worker tasks; replaced with durable PostgreSQL outbox and retries. Last-millisecond crash after copy may still cause a duplicate (Telegram has no idempotency keys).
- Original topic lock dictionary cleanup could race with waiters. Added guarded refcounts.
- Original topic creation and DB mapping could leave an orphan on failure. Added compensating cleanup and SQL transaction for the mapping.
- Original reopen function cleared mappings on any Telegram error. Only provable missing topics trigger recreation; rate limits/transient failures do not destroy mapping.
- Original `create_topic` favored the old selected game over a newly passed deep-link game. Game choice is now persisted before topic creation.
- Original `/start`/game selection were background fire-and-forget tasks. They are awaited and use per-customer synchronization.
- Original `/broadcast` sent to all users without prior consent or durable progress. New opt-in broadcast creates draft, owner confirmation and persistent outbox jobs.
- Original queue lacked audit logs and instrumentation. Added `/health`, structured logs and activity records.
- Original privacy retention statements had no automated implementation. Added optional disabled-by-default cleanup.

## New functions

- Pinned customer profile per topic with active game, multiple saved game usernames, labels, notes, assigned staff, status, manually recorded deposit totals and optional marketing consent.
- Automatic topic title updates and label prioritization (VIP > Depositor > Regular > Freeplay > New).
- Multiple game-account records, search, collision warning, staff note history.
- Staff assignment, workflow state, shortcuts, staff-entered deposit references.
- Explicit messages for final delivery failure, log counters, opt-in/opt-out checks.

## Items still requiring production validation or separate projects

- Actual Telegram API admin permissions, Railway lifecycle, PostgreSQL migration against the **real database**, and end-to-end message/media copy.
- Long-running flood limits and mass customer concurrency testing.
- Distributed locking and cross-worker role-cache invalidation if the service ever scales beyond one bot worker.
- Privacy/retention policies, backup controls and access reviews must match actual business practices.
- External payment verification cannot happen without a properly authenticated payment provider. Recording a deposit in the bot is only staff-entered history.
- A Meta Business Suite-style web UI and AI-generated replies are separate product phases; not in this bot deployment.


## October 2026 — Case-insensitive CRM and automatic game accounts

Customer-facing game messages sent **by authorized staff inside a customer's Telegram support topic** are now inspected automatically. When a message contains exactly one `Username:` and one `Password:` line, the bot detects a known game suffix and saves the username/password to PostgreSQL while keeping the normal message-delivery behavior.

Recognized suffixes: `3s` Orionstars, `3fk` Firekirin, `3v` Vblink, `3u` Ultrapanda, `3r` Riversweeps, `3j` Juwa, `3gv` GameVault, `3mw` Milkyway, `3cm` CashMaster. Multiple consecutive 3s are supported, such as `Player333fk`. Any player-name prefix is accepted; `Dummy` is just a sample. For custom games, add a `Game: Custom Game` line.

```text
👤 Username: Michael333fk
🔑 Password: 009988
Log in and let me know if you need help ❤️✨
```

Use `/accounts` inside the customer topic to see saved game accounts and passwords, `/label Special Customer` to save a custom label (spaces allowed), `/unlabel special customer` to remove it, and `/setaccount Juwa player123 009988` to add an account manually. Labels, game selection/search, account lookup, and command names match case-insensitively; passwords keep their exact case. Passwords are stored as **plaintext** by request. Keep your Railway database and staff group access restricted.

**Deployment:** PostgreSQL schema migrations add an account-password column and an automatic-capture deduplication table. Always back up the database before rollout. The source-only rollback branch `backup/pre-crm-20261011` does not back up or restore PostgreSQL.
