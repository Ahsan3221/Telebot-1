# BotFather setup for WishWheel Support

**Name:** WishWheel Support

**About (example):** Customer support, game selection and account assistance for WishWheel.

**Description (example):** Message our support team here. Your conversation is routed privately to authorized staff. Use /privacy for data practices, /delete_data for deletion requests, or /optout to stop optional updates.

**Suggested public commands:**

- `start` — Start support
- `help` — See help
- `games` — Choose a game
- `support` — Reach staff
- `privacy` — Privacy policy
- `delete_data` — Delete your records
- `optin` — Subscribe to updates
- `optout` — Stop optional updates

Set the Telegram privacy policy URL to your actual published policy. The internal staff commands are registered for the support chat by the bot, not public customer menu.

**Important:** Add bot as admin in the private topics-enabled support supergroup. Enable `can_manage_topics`, pin, message and delete rights as appropriate. Confirm it can receive normal staff messages in each topic (Privacy Mode may need to be disabled in BotFather). Do not give bot tokens to staff.
