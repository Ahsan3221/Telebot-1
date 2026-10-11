# Broadcast eligibility after /start

**Operational behavior**
- A first-time user who sends `/start` is added to the bot's eligible broadcast audience automatically, after the support record is created.
- The customer receives **no extra opt-in notice, popup or buttons**. The existing welcome text and bonus/game selection remain unchanged.
- `/optin` is no longer a registered customer command.
- `/optout` is not shown in the bot's command menu; the underlying command still functions when a customer types it explicitly, so requests to stop updates continue to be respected.
- Blocked customers, customers who opted out, and failed Telegram delivery targets are excluded as before. Sending a broadcast still requires the authorized staff member to confirm a preview; broadcast messages are paced with at least one second between sends.

**Legacy user limitation**
Existing `marketing_opt_in=FALSE` records are kept excluded. The old data does not distinguish a default false value from a customer's previous `/optout`. Automatically forcing every legacy false record to true would silently override past opt-outs. A customer who wants to resume messages can request re-subscription through support; no automatic backfill is performed.

**After Railway deployment**
Check the bot's /start with a test Telegram account not previously registered in that bot, then use /broadcast from the authorized staff group. Check the preview recipient count, send a small test first and verify that no legacy opt-outs or blocked accounts are included. Source updates alone do not confirm that Railway has redeployed.
