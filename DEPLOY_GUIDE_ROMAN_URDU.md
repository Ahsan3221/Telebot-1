# WishWheel (Telebot-1) — Railway PostgreSQL Deployment

Bhai, ye **Telebot-1** ka WishWheel bot hai jo Railway PostgreSQL use karta hai. Purane SQLite files sirf backup branch par hain.

1. Railway PostgreSQL ka snapshot lo — source-code backup se DB restore nahi hoti.
2. Railway → WishWheel bot → Settings/Source mein `Telebot-1` aur branch `main` check karo.
3. Purane `BOT_TOKEN`, `SUPPORT_GROUP_ID` aur `DATABASE_URL` **same** rakho.
4. Owner commands ke liye `OWNER_IDS` mein apni numeric Telegram ID add karo. `AUTHORIZED_STAFF_IDS` as needed.
5. Railway deployment logs check karo. Bot has to connect via PostgreSQL, **not SQLite**.
6. Existing customer ka topic kholo. `/profile` aur `/accounts` test karo, phir `/label VIP`.
7. Topic mein `👤 Username:Player333fk` aur `🔑 Password: 009988` wala normal message bhejo. Bot ko Firekirin save karna chahiye.
8. Game selection mein CashMaster verify karo.
9. Agar rollback karna pade to `backup/pre-postgres-crm-20261011` branch se source restore karo. Database separately backup/restore karo.

Existing PostgreSQL tables `users`, `topics`, `staff` remain. CRM tables: `wishwheel_*`.

Purane `support.db`, WAL, SHM, `support_data.json`, Windows .bat aur old `Procfile.txt` `main` par nahi rahenge. Backup branch mein safe hain.

**Security:** Saved game passwords PostgreSQL mein plaintext hain, as requested. DB access restricted rakho. Old files Git history mein ab bhi ho sakte hain.
