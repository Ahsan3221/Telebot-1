# WishWheel Support Bot — Privacy Policy (DRAFT)

**This is a draft template, NOT a ready-to-publish legal policy.** Replace the bracketed details and verify the statements against your actual business practices before publication.

**Operator:** [Legal operator name and jurisdiction]

**Privacy contact:** [Contact email or accessible support channel]

**Effective date:** [YYYY-MM-DD]

## What the bot processes

For the purpose of responding to customer inquiries, the bot processes Telegram numeric user IDs, display names, public handles (if any), customer chat messages and media shared with support, support topic identifiers, game selections, bonus selections and optional referral/traffic-source data. Authorized staff may save optional game-account usernames, labels, notes, workflow status, and manually entered payment references/amounts.

## How this data is used

We use this information to route support conversations to our staff, maintain customer support records, identify the relevant game account when assisting a customer, prevent duplicate work, and maintain a record of staff actions. Access is restricted to explicitly authorized staff IDs and the operator's administrators.

## Optional messages

The bot will send optional broadcasts only if a customer chooses `/optin`. Customers can withdraw permission with `/optout` at any time. These choices do not affect their ability to request support.

## Service providers and storage

The bot uses Telegram for messaging and [Railway/actual hosting provider] for its application and [actual PostgreSQL service/provider] for database storage. Telegram retains messages according to Telegram's separate terms and policies. Staff devices, business records, exports, backups, and other data stores may require separate deletion procedures.

## Retention

[Insert the actual retention period and legal exceptions for your business.] The bot's optional `RETENTION_DAYS` parameter can automatically delete inactive database records and their support forum topics if successfully configured; by default this feature is **disabled**. Align production configuration with the published policy.

## Data access and deletion

Customers can request to delete their bot support profile through `/delete_data`, which asks for confirmation. The bot attempts to delete its Telegram support topic first, then deletes its own related database records. If Telegram topic deletion fails, the database record is retained and the customer is told staff action is required. This does not erase separate backups, devices or externally retained copies. Contact [contact method] for additional privacy requests.

## Security

The bot uses role-based access controls and a private support group. No service can promise absolute security. Do not send passwords, one-time codes, full card numbers, or other sensitive account secrets to support.

## Changes

We may update this notice to reflect changes in our practices or applicable law; updates are posted at [public URL].
