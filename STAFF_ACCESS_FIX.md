# Staff Authorization Recovery

Staff role checks are shared across CRM commands, customer-topic replies, broadcast commands, broadcast confirmation callbacks, and Winning Wave legacy campaign/dashboard handlers.

## What qualifies as staff?

- A Telegram ID in Railway `OWNER_IDS`, `AUTHORIZED_STAFF_IDS`, `STAFF_IDS`, `ADMIN_IDS`, `MANAGER_IDS`, `TEAM_IDS`, `EMPLOYEE_IDS` or other *Telegram human ID* variables ending `_ID` or `_IDS` (for example `SALMAN_ID`, `MOHEET_ID`). Infrastructure ID variables such as `SUPPORT_GROUP_ID`, `BOT_ID`, `APP_ID` and `CAMPAIGN_ID` are explicitly excluded.
- An active entry in the existing staff database (including staff previously added with `/addstaff`). The list refreshes automatically if a new staff member is not found in the startup cache.
- A current Telegram administrator or owner of the configured support group (verified through Telegram).

Ordinary, unlisted Telegram group members do **not** receive broadcast access automatically.

## Team troubleshooting

Have a staff member issue this in the bot's **configured support group**:

```text
/staffcheck
```

The response shows their actual Telegram ID, matching Railway variable name(s), whether their staff database record is active, and whether they have permission. It works even if the user is not authorized yet. It does not expose secrets or other staff members' IDs.

If the ID is missing, an authorized member can add it using:

```text
/addstaff <telegram_user_id> Employee Name
```

Alternatively, add the ID to Railway `AUTHORIZED_STAFF_IDS` and redeploy. ID fields can contain comma-separated, whitespace-separated, or JSON-list-style numeric Telegram user IDs.

**Anonymous admin:** If Telegram posts as the group identity instead of a human Telegram account, the bot cannot determine the human's ID. Disable "Remain Anonymous" for that admin, then use `/staffcheck` and `/broadcast` again.

## Safety and deployment

Source commit changes do not prove Railway has deployed. After pushing, confirm the running Railway deployment SHA. Do not create additional poller replicas: one bot token must have one Telegram `getUpdates` polling process.

User/customer messages and your existing database records are unchanged by this patch. Broadcast pacing and delivery-status commands are unchanged.
