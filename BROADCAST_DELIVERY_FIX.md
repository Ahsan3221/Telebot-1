# Broadcast delivery restoration and diagnostics

## What changed
- `/broadcast <message>` immediately queues the registered customer audience (the previous simple workflow), without a separate opt-in command or a confirmation button.
- All registered users are eligible except staff-blocked customers, known Telegram-unreachable users, and customers whose explicit stop requests have been recorded. Past default opt-in flags do not filter ordinary registered customers.
- Older/inactive customer topics are processed before more recently active topics, so recently active discussions tend to remain nearer the top in Telegram. Telegram controls actual topic ordering, so this is best-effort rather than a guarantee.
- At least one second separates broadcasts to customers, with durable retries on transient Telegram errors and respect for RetryAfter.
- Successfully sent customer messages receive a separate **staff-topic mirror**, queued durably. Failure to mirror will not resend the message to a player. Closed or missing topics are skipped and reported.
- Customers who have blocked the bot are marked as unreachable following a definitive Telegram rejection; they are skipped in subsequent broadcasts until their next direct interaction.
- The support-group `/block` and `/unblock` commands remain available to authorized staff. Existing branding, games, topic IDs and CRM data remain untouched.

## Staff commands
```text
/broadcast Hello {name}, here is today's update.
/bstatus
/bstatus 45
/bcheck 123456789
/bcheck 123456789 45
```

`/bstatus` distinguishes queued/pending from successfully sent and permanently failed. `/bcheck` shows whether a particular registered player was included, the retry count, and last Telegram error. If a bot has not recorded the player ID, no broadcast can be sent to it.

## Railway conflicts
`Conflict: terminated by other getUpdates request` is emitted by Telegram when multiple processes poll with one bot token. Keep exactly one running poller for each unique token (one Railway replica is not sufficient if a second deployment, service or host also uses that token). Redeploy overlap can be transient. This source update does not terminate other Railway services.

## Deployment
Keep the PostgreSQL database and existing environment variables. For older SQLite `Telebot`, retain the persistent SQLite database. Back up data before deploy; added columns and one broadcast mirror table are created automatically. Follow the latest CI results and confirm Railway has actually restarted on the new main commit.

A queued broadcast is not a delivery guarantee: users can block the bot, deactivate accounts, or Telegram can reject/limit sends. Review `/bstatus` rather than assuming a recipient preview count means 100% delivered.
