"""Transactional PostgreSQL access; namespaced tables preserve old bot records."""
import asyncio
import logging
from datetime import datetime, timezone
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

log = logging.getLogger(__name__)
U = "users"
T = "topics"
S = "staff"
A = "wishwheel_accounts"
L = "wishwheel_labels"
N = "wishwheel_notes"
D = "wishwheel_deposits"
H = "wishwheel_activity"
O = "wishwheel_outbox"
Q = "wishwheel_shortcuts"
B = "wishwheel_broadcasts"
C = "wishwheel_account_captures"
M = B + "_mirrors"


class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None

    def connect(self):
        if self.pool is None:
            self.pool = pool.ThreadedConnectionPool(1, 10, dsn=self.dsn, cursor_factory=RealDictCursor)
        return self

    def close(self):
        if self.pool:
            self.pool.closeall()
            self.pool = None

    def run(self, fn, *args):
        self.connect()
        conn = self.pool.getconn()
        try:
            with conn.cursor() as cur:
                result = fn(cur, *args)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            self.pool.putconn(conn)

    async def call(self, fn, *args):
        return await asyncio.to_thread(self.run, fn, *args)

    def migrate(self):
        def script(cur):
            # CREATE/ALTER remain additive: existing customers and topic IDs survive.
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {U} (
                user_id BIGINT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '', topic_id BIGINT,
                topic_status TEXT NOT NULL DEFAULT 'open', game TEXT, source TEXT,
                bonus TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            for column, definition in (
                ("source", "TEXT"), ("bonus", "TEXT"),
                ("topic_status", "TEXT NOT NULL DEFAULT 'open'"),
                ("primary_label", "TEXT NOT NULL DEFAULT 'new'"),
                ("primary_label_display", "TEXT"),
                ("workflow_status", "TEXT NOT NULL DEFAULT 'new'"),
                ("assigned_staff", "BIGINT"),
                ("profile_message_id", "BIGINT"),
                ("marketing_opt_in", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("marketing_opted_out", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("is_blocked", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("bot_blocked", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ):
                cur.execute(f"ALTER TABLE {U} ADD COLUMN IF NOT EXISTS {column} {definition}")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {T} (
                topic_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES {U}(user_id) ON DELETE CASCADE)""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {S} (
                user_id BIGINT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                added_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute(f"ALTER TABLE {S} ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'staff'")
            cur.execute(f"ALTER TABLE {S} ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {A} (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES {U}(user_id) ON DELETE CASCADE,
                game TEXT NOT NULL, account_username TEXT NOT NULL, changed_by BIGINT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (user_id,game))""")
            cur.execute(f"ALTER TABLE {A} ADD COLUMN IF NOT EXISTS account_password TEXT")
            cur.execute(f"CREATE INDEX IF NOT EXISTS wishwheel_accounts_lookup ON {A}(LOWER(account_username))")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {C} (
                source_chat_id BIGINT NOT NULL, source_message_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL REFERENCES {U}(user_id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(source_chat_id, source_message_id))""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {L} (
                user_id BIGINT NOT NULL REFERENCES {U}(user_id) ON DELETE CASCADE,
                label TEXT NOT NULL, changed_by BIGINT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(user_id,label))""")
            cur.execute(f"ALTER TABLE {L} ADD COLUMN IF NOT EXISTS label_display TEXT")
            cur.execute(f"UPDATE {L} SET label_display=label WHERE label_display IS NULL")
            cur.execute(f"CREATE INDEX IF NOT EXISTS wishwheel_labels_lookup ON {L}(label)")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {N} (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES {U}(user_id) ON DELETE CASCADE,
                body TEXT NOT NULL, author_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute(f"CREATE INDEX IF NOT EXISTS wishwheel_notes_user_time ON {N}(user_id, created_at DESC)")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {D} (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES {U}(user_id) ON DELETE CASCADE,
                amount NUMERIC(12,2) NOT NULL CHECK (amount > 0), reference TEXT NOT NULL UNIQUE,
                entered_by BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {H} (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES {U}(user_id) ON DELETE CASCADE,
                actor_id BIGINT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute(f"CREATE INDEX IF NOT EXISTS wishwheel_activity_user_time ON {H}(user_id, created_at DESC)")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {O} (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES {U}(user_id) ON DELETE CASCADE,
                direction TEXT NOT NULL CHECK (direction IN ('customer','staff','broadcast','shortcut')),
                source_chat_id BIGINT, source_message_id BIGINT, body TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                locked_until TIMESTAMPTZ,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), delivered_at TIMESTAMPTZ)""")
            cur.execute(f"CREATE INDEX IF NOT EXISTS wishwheel_outbox_pending ON {O}(state, next_attempt, id)")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {Q} (
                shortcut_key TEXT PRIMARY KEY, body TEXT NOT NULL,
                changed_by BIGINT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {B} (
                id BIGSERIAL PRIMARY KEY, author_id BIGINT NOT NULL, body TEXT NOT NULL,
                label TEXT, state TEXT NOT NULL DEFAULT 'draft',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), confirmed_at TIMESTAMPTZ)""")
            # Mirror jobs are added only after the private Telegram send succeeded.
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {M} (
                broadcast_job_id BIGINT PRIMARY KEY REFERENCES {O}(id) ON DELETE CASCADE,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                locked_until TIMESTAMPTZ, last_error TEXT NOT NULL DEFAULT '')""")
            # One active forum topic per user. Clean up stale historical topic aliases.
            cur.execute(f"CREATE INDEX IF NOT EXISTS wishwheel_users_topic_lookup ON {U}(topic_id) WHERE topic_id IS NOT NULL")
            cur.execute(f"CREATE INDEX IF NOT EXISTS wishwheel_users_activity ON {U}(last_seen)")
        self.run(script)

    def seed(self, owners, staff):
        def fn(cur):
            for uid in owners:
                cur.execute(f"""INSERT INTO {S}(user_id,name,role,active) VALUES (%s,'Owner','owner',TRUE)
                    ON CONFLICT(user_id) DO UPDATE SET role='owner',active=TRUE""", (uid,))
            for uid in staff - owners:
                # Previously revoked users are not silently re-enabled after restart.
                cur.execute(f"""INSERT INTO {S}(user_id,name,role,active) VALUES (%s,'Staff','staff',TRUE)
                    ON CONFLICT(user_id) DO NOTHING""", (uid,))
        self.run(fn)

    async def roles(self):
        def fn(cur):
            cur.execute(f"SELECT user_id, role FROM {S} WHERE active = TRUE")
            return {int(x['user_id']): x['role'] for x in cur.fetchall()}
        return await self.call(fn)

    async def staff_set(self, uid, name, role, active):
        def fn(cur):
            cur.execute(f"""INSERT INTO {S}(user_id,name,role,active) VALUES(%s,%s,%s,%s)
                ON CONFLICT(user_id) DO UPDATE SET name=EXCLUDED.name,
                role=EXCLUDED.role, active=EXCLUDED.active""", (uid,name,role,active))
        await self.call(fn)

    async def upsert_user(self, user, *, game=None, source=None, bonus=None, touch=True):
        def fn(cur):
            cur.execute(f"""INSERT INTO {U}(user_id,name,username,game,source,bonus)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(user_id) DO UPDATE SET
                    name=EXCLUDED.name, username=EXCLUDED.username,
                    game=COALESCE(EXCLUDED.game,{U}.game),
                    source=COALESCE(EXCLUDED.source,{U}.source),
                    bonus=COALESCE(EXCLUDED.bonus,{U}.bonus),
                    last_seen=CASE WHEN %s THEN NOW() ELSE {U}.last_seen END,
                    bot_blocked=FALSE
                RETURNING *""", (user.id, user.full_name or user.first_name or 'Unknown',
                                  user.username or '', game,source,bonus,touch))
            return dict(cur.fetchone())
        return await self.call(fn)

    async def user(self, user_id):
        def fn(cur):
            cur.execute(f"SELECT * FROM {U} WHERE user_id=%s", (user_id,))
            return cur.fetchone()
        return await self.call(fn)

    async def by_topic(self, topic_id):
        def fn(cur):
            cur.execute(f"SELECT * FROM {U} WHERE topic_id=%s", (topic_id,))
            return cur.fetchone()
        return await self.call(fn)

    async def assign_topic(self, user_id, topic_id):
        def fn(cur):
            cur.execute(f"SELECT topic_id FROM {U} WHERE user_id=%s FOR UPDATE", (user_id,))
            row=cur.fetchone()
            if not row:
                raise ValueError('Customer must exist before topic assignment')
            old=row['topic_id']
            if old and old != topic_id:
                cur.execute(f"DELETE FROM {T} WHERE topic_id=%s", (old,))
            cur.execute(f"UPDATE {U} SET topic_id=%s,topic_status='open', profile_message_id=NULL WHERE user_id=%s", (topic_id,user_id))
            cur.execute(f"INSERT INTO {T}(topic_id,user_id) VALUES(%s,%s) ON CONFLICT(topic_id) DO UPDATE SET user_id=EXCLUDED.user_id", (topic_id,user_id))
        await self.call(fn)

    async def mark_topic(self, user_id, status):
        def fn(cur):
            cur.execute(f"UPDATE {U} SET topic_status=%s WHERE user_id=%s", (status,user_id))
        await self.call(fn)

    async def profile_id(self, user_id, message_id):
        def fn(cur):
            cur.execute(f"UPDATE {U} SET profile_message_id=%s WHERE user_id=%s", (message_id,user_id))
        await self.call(fn)

    async def profile_parts(self, user_id):
        def fn(cur):
            cur.execute(f"SELECT game,account_username FROM {A} WHERE user_id=%s ORDER BY game",(user_id,))
            accounts=cur.fetchall()
            cur.execute(f"SELECT label,label_display FROM {L} WHERE user_id=%s ORDER BY label",(user_id,))
            labels=cur.fetchall()
            cur.execute(f"SELECT body,author_id,created_at FROM {N} WHERE user_id=%s ORDER BY id DESC LIMIT 3",(user_id,))
            notes=cur.fetchall()
            cur.execute(f"SELECT COUNT(*) AS count, COALESCE(SUM(amount),0) AS amount FROM {D} WHERE user_id=%s",(user_id,))
            deposits=cur.fetchone()
            return accounts,labels,notes,deposits
        return await self.call(fn)

    async def set_account(self, uid, game, account, actor, password=None, source_chat=None, source_message=None):
        """Manual or idempotent automatic save. Password is plaintext by request."""
        def fn(cur):
            # Serialize updates to the same user's account list, including casing variants.
            cur.execute(f"SELECT user_id FROM {U} WHERE user_id=%s FOR UPDATE", (uid,))
            if not cur.fetchone():
                raise ValueError('Customer record does not exist')
            if source_chat is not None and source_message is not None:
                cur.execute(f"""INSERT INTO {C}(source_chat_id, source_message_id, user_id)
                    VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING source_message_id""",
                    (source_chat,source_message,uid))
                if not cur.fetchone():
                    return None  # This staff message has already been captured.
            cur.execute(f"""SELECT DISTINCT user_id FROM {A}
                WHERE LOWER(account_username)=LOWER(%s) AND LOWER(game)=LOWER(%s)
                  AND user_id<>%s LIMIT 10""", (account,game,uid))
            duplicates=[int(x['user_id']) for x in cur.fetchall()]
            cur.execute(f"""SELECT id,account_username,account_password FROM {A}
                WHERE user_id=%s AND LOWER(game)=LOWER(%s)
                ORDER BY updated_at DESC,id DESC LIMIT 1 FOR UPDATE""", (uid,game))
            current=cur.fetchone()
            if current:
                # Preserve prior password only for the very same username if no new one was supplied.
                replacement_password=(password if password is not None else
                    current['account_password'] if current['account_username'].casefold()==account.casefold() else None)
                cur.execute(f"""UPDATE {A}
                    SET game=%s, account_username=%s, account_password=%s,
                        changed_by=%s, updated_at=NOW()
                    WHERE id=%s""", (game,account,replacement_password,actor,current['id']))
            else:
                cur.execute(f"""INSERT INTO {A}(user_id,game,account_username,account_password,changed_by)
                    VALUES(%s,%s,%s,%s,%s)""",(uid,game,account,password,actor))
            # Show the latest game in the customer profile.
            cur.execute(f"UPDATE {U} SET game=%s WHERE user_id=%s", (game,uid))
            self._audit(cur,uid,actor,'auto_account' if source_message is not None else 'set_account',
                        f'{game}: {account}')  # Intentionally omit plaintext password from audit/logs.
            return duplicates
        return await self.call(fn)

    async def account_details(self, uid):
        def fn(cur):
            cur.execute(f"""SELECT game,account_username,account_password,updated_at
                FROM {A} WHERE user_id=%s ORDER BY game""",(uid,))
            return cur.fetchall()
        return await self.call(fn)

    async def delete_account(self,uid,game,actor):
        def fn(cur):
            cur.execute(f"DELETE FROM {A} WHERE user_id=%s AND LOWER(game)=LOWER(%s) RETURNING id",(uid,game))
            deleted=bool(cur.fetchone())
            if deleted:
                self._audit(cur,uid,actor,'delete_account',game)
            return deleted
        return await self.call(fn)

    async def find_account(self, account):
        def fn(cur):
            cur.execute(f"""SELECT a.game,a.account_username,u.user_id,u.name,u.topic_id
                FROM {A} a JOIN {U} u ON u.user_id=a.user_id
                WHERE LOWER(a.account_username)=LOWER(%s) LIMIT 20""",(account,))
            return cur.fetchall()
        return await self.call(fn)

    async def set_label(self,uid,label,actor,remove=False,display=None):
        """Label key is lower/casefolded, display retains user's custom capitalization."""
        from crm import label_display as format_label
        shown=format_label(label,display)
        def fn(cur):
            if remove:
                cur.execute(f"DELETE FROM {L} WHERE user_id=%s AND LOWER(label)=LOWER(%s)",(uid,label))
            else:
                cur.execute(f"""INSERT INTO {L}(user_id,label,label_display,changed_by)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(user_id,label) DO UPDATE
                    SET label_display=EXCLUDED.label_display,
                        changed_by=EXCLUDED.changed_by, updated_at=NOW()""",(uid,label,shown,actor))
            if not remove:
                primary=label; primary_display=shown
            else:
                cur.execute(f"""SELECT label,label_display FROM {L} WHERE user_id=%s
                    ORDER BY updated_at DESC,label LIMIT 1""",(uid,))
                row=cur.fetchone()
                primary=row['label'] if row else 'new'
                primary_display=format_label(row['label'],row['label_display']) if row else None
            cur.execute(f"UPDATE {U} SET primary_label=%s,primary_label_display=%s WHERE user_id=%s",
                        (primary,primary_display,uid))
            self._audit(cur,uid,actor,'remove_label' if remove else 'add_label',label)
            return primary
        return await self.call(fn)

    def _audit(self,cur,uid,actor,action,detail=''):
        cur.execute(f"INSERT INTO {H}(user_id,actor_id,action,detail) VALUES(%s,%s,%s,%s)", (uid,actor,action,detail[:400]))

    async def note(self,uid,body,author):
        def fn(cur):
            cur.execute(f"INSERT INTO {N}(user_id,body,author_id) VALUES(%s,%s,%s)",(uid,body,author))
            self._audit(cur,uid,author,'note_added','(note content not duplicated in audit)')
        await self.call(fn)

    async def workflow(self,uid,status,actor):
        def fn(cur):
            cur.execute(f"UPDATE {U} SET workflow_status=%s WHERE user_id=%s",(status,uid))
            self._audit(cur,uid,actor,'workflow',status)
        await self.call(fn)

    async def assign(self,uid,staff_id,actor):
        def fn(cur):
            cur.execute(f"UPDATE {U} SET assigned_staff=%s WHERE user_id=%s",(staff_id,uid))
            self._audit(cur,uid,actor,'assign_staff',str(staff_id or 'none'))
        await self.call(fn)

    async def deposit(self,uid,amount,reference,actor):
        def fn(cur):
            cur.execute(f"INSERT INTO {D}(user_id,amount,reference,entered_by) VALUES(%s,%s,%s,%s)",(uid,amount,reference,actor))
            # Keep existing custom/VIP primary label; add Depositor as secondary tag.
            cur.execute(f"""INSERT INTO {L}(user_id,label,label_display,changed_by)
                VALUES(%s,'depositor','💰 Depositor',%s)
                ON CONFLICT(user_id,label) DO UPDATE SET changed_by=EXCLUDED.changed_by,updated_at=NOW()""",(uid,actor))
            cur.execute(f"""UPDATE {U} SET primary_label='depositor',
                primary_label_display='💰 Depositor'
                WHERE user_id=%s AND (primary_label IS NULL OR primary_label='new')""",(uid,))
            self._audit(cur,uid,actor,'record_deposit',f'Amount {amount}, reference {reference}')
        await self.call(fn)

    async def history(self,uid):
        def fn(cur):
            cur.execute(f"SELECT actor_id,action,detail,created_at FROM {H} WHERE user_id=%s ORDER BY id DESC LIMIT 15",(uid,))
            return cur.fetchall()
        return await self.call(fn)

    async def delete_user(self,uid):
        def fn(cur):
            cur.execute(f"DELETE FROM {T} WHERE user_id=%s", (uid,))
            cur.execute(f"DELETE FROM {U} WHERE user_id=%s RETURNING topic_id",(uid,))
            return cur.fetchone()
        return await self.call(fn)

    async def set_blocked(self,uid,blocked,actor,reason=''):
        """Persist support access restriction with an audit entry."""
        def fn(cur):
            cur.execute(f"UPDATE {U} SET is_blocked=%s WHERE user_id=%s RETURNING user_id",
                        (bool(blocked),uid))
            if not cur.fetchone():
                return False
            self._audit(cur,uid,actor,'blocked' if blocked else 'unblocked',reason or '')
            return True
        return await self.call(fn)

    async def opt_in(self,uid,enabled):
        def fn(cur):
            cur.execute(f"UPDATE {U} SET marketing_opt_in=%s,marketing_opted_out=%s WHERE user_id=%s",
                        (bool(enabled),not bool(enabled),uid))
        await self.call(fn)

    async def outbox_enqueue(self,uid,direction,source_chat=None,source_message=None,body=None,key=None):
        if key is None:
            key=f'{direction}:{source_chat}:{source_message}'
        def fn(cur):
            cur.execute(f"""INSERT INTO {O}(user_id,direction,source_chat_id,source_message_id,body,idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id""",
                (uid,direction,source_chat,source_message,body,key))
            row=cur.fetchone()
            return row['id'] if row else None
        return await self.call(fn)

    async def outbox_claim(self):
        def fn(cur):
            # Lease allows another worker to recover an interrupted delivery after restart.
            cur.execute(f"""SELECT * FROM {O} WHERE
                ((state='pending' AND next_attempt<=NOW()) OR
                 (state='processing' AND locked_until < NOW()))
                ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""")
            row=cur.fetchone()
            if row:
                cur.execute(f"UPDATE {O} SET state='processing', locked_until=NOW()+INTERVAL '2 minutes',attempts=attempts+1 WHERE id=%s",(row['id'],))
            return row
        return await self.call(fn)

    async def outbox_result(self,job_id,success,error='',retry_seconds=0):
        def fn(cur):
            if success:
                cur.execute(f"""UPDATE {O} SET state='sent',delivered_at=NOW(),
                    locked_until=NULL,last_error='' WHERE id=%s RETURNING direction""",(job_id,))
                sent=cur.fetchone()
                if sent and sent['direction']=='broadcast':
                    cur.execute(f"INSERT INTO {M}(broadcast_job_id) VALUES(%s) ON CONFLICT DO NOTHING",(job_id,))
            elif retry_seconds:
                cur.execute(f"""UPDATE {O} SET state='pending', locked_until=NULL,
                    next_attempt=NOW()+(%s * INTERVAL '1 second'),last_error=%s WHERE id=%s""",
                    (retry_seconds,error[:300],job_id))
            else:
                cur.execute(f"UPDATE {O} SET state='dead',locked_until=NULL,last_error=%s WHERE id=%s",(error[:300],job_id))
        await self.call(fn)

    async def mark_bot_blocked(self,uid,blocked=True):
        def fn(cur):
            cur.execute(f"UPDATE {U} SET bot_blocked=%s WHERE user_id=%s",(bool(blocked),uid))
        await self.call(fn)

    async def outbox_claim_mirror(self):
        """Mirrors successful broadcasts; never repeats the customer send."""
        def fn(cur):
            cur.execute(f"""SELECT m.broadcast_job_id AS id,m.attempts,o.user_id,o.body,
                u.topic_id,u.topic_status
                FROM {M} m JOIN {O} o ON o.id=m.broadcast_job_id
                JOIN {U} u ON u.user_id=o.user_id
                WHERE (m.state='pending' AND m.next_attempt<=NOW())
                   OR (m.state='processing' AND m.locked_until<NOW())
                ORDER BY m.broadcast_job_id ASC
                LIMIT 1 FOR UPDATE SKIP LOCKED""")
            row=cur.fetchone()
            if row:
                cur.execute(f"""UPDATE {M} SET state='processing',attempts=attempts+1,
                    locked_until=NOW()+INTERVAL '2 minutes' WHERE broadcast_job_id=%s""",(row['id'],))
            return row
        return await self.call(fn)

    async def outbox_mirror_result(self,job_id,success,error='',retry_seconds=0,skipped=False):
        def fn(cur):
            if skipped:
                cur.execute(f"UPDATE {M} SET state='skipped',locked_until=NULL WHERE broadcast_job_id=%s",(job_id,))
            elif success:
                cur.execute(f"UPDATE {M} SET state='sent',locked_until=NULL,last_error='' WHERE broadcast_job_id=%s",(job_id,))
            elif retry_seconds:
                cur.execute(f"""UPDATE {M} SET state='pending',locked_until=NULL,
                    next_attempt=NOW()+(%s * INTERVAL '1 second'),last_error=%s
                    WHERE broadcast_job_id=%s""",(retry_seconds,error[:300],job_id))
            else:
                cur.execute(f"UPDATE {M} SET state='dead',locked_until=NULL,last_error=%s WHERE broadcast_job_id=%s",(error[:300],job_id))
        await self.call(fn)

    async def broadcast_report(self,bid=None):
        def fn(cur):
            if bid is None:
                cur.execute(f"SELECT id,state FROM {B} ORDER BY id DESC LIMIT 1")
            else:
                cur.execute(f"SELECT id,state FROM {B} WHERE id=%s",(bid,))
            record=cur.fetchone()
            if not record:return None
            pattern=f"broadcast:{record['id']}:%"
            cur.execute(f"""SELECT state,COUNT(*) c FROM {O}
                WHERE direction='broadcast' AND idempotency_key LIKE %s
                GROUP BY state""",(pattern,))
            delivery={r['state']:r['c'] for r in cur.fetchall()}
            cur.execute(f"""SELECT m.state,COUNT(*) c FROM {M} m JOIN {O} o
                ON o.id=m.broadcast_job_id
                WHERE o.idempotency_key LIKE %s GROUP BY m.state""",(pattern,))
            mirrors={r['state']:r['c'] for r in cur.fetchall()}
            cur.execute(f"""SELECT user_id,last_error FROM {O}
                WHERE direction='broadcast' AND idempotency_key LIKE %s
                AND state='dead' ORDER BY id LIMIT 3""",(pattern,))
            return dict(id=record['id'],status=record['state'],delivery=delivery,
                        mirror=mirrors,errors=cur.fetchall())
        return await self.call(fn)

    async def broadcast_check(self,uid,bid=None):
        def fn(cur):
            cur.execute(f"SELECT user_id,name,topic_id,is_blocked,bot_blocked FROM {U} WHERE user_id=%s",(uid,))
            recipient=cur.fetchone()
            if not recipient:return None
            if bid is None:
                cur.execute(f"""SELECT o.id,o.state,o.attempts,o.last_error,
                    m.state AS mirror_state,m.last_error AS mirror_error
                    FROM {O} o LEFT JOIN {M} m ON m.broadcast_job_id=o.id
                    WHERE o.user_id=%s AND o.direction='broadcast' ORDER BY o.id DESC LIMIT 1""",(uid,))
            else:
                cur.execute(f"""SELECT o.id,o.state,o.attempts,o.last_error,
                    m.state AS mirror_state,m.last_error AS mirror_error
                    FROM {O} o LEFT JOIN {M} m ON m.broadcast_job_id=o.id
                    WHERE o.user_id=%s AND o.direction='broadcast'
                    AND o.idempotency_key=%s ORDER BY o.id DESC LIMIT 1""",(uid,f'broadcast:{bid}:{uid}'))
            return dict(user=recipient,delivery=cur.fetchone())
        return await self.call(fn)

    async def outbox_counts(self):
        def fn(cur):
            cur.execute(f"SELECT state,COUNT(*) AS c FROM {O} GROUP BY state")
            return {x['state']:x['c'] for x in cur.fetchall()}
        return await self.call(fn)

    async def stats(self):
        def fn(cur):
            cur.execute(f"SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE created_at>=NOW()-INTERVAL '1 day') AS today, COUNT(*) FILTER (WHERE topic_status='open' AND topic_id IS NOT NULL) AS open FROM {U}")
            main=cur.fetchone()
            cur.execute(f"SELECT game, COUNT(*) c FROM {U} WHERE game IS NOT NULL GROUP BY game ORDER BY c DESC LIMIT 10")
            games=cur.fetchall()
            cur.execute(f"SELECT primary_label,COUNT(*) c FROM {U} GROUP BY primary_label")
            labels=cur.fetchall()
            return main,games,labels
        return await self.call(fn)

    async def shortcuts(self):
        def fn(cur):
            cur.execute(f"SELECT shortcut_key,body FROM {Q} ORDER BY shortcut_key")
            return cur.fetchall()
        return await self.call(fn)

    async def shortcut_get(self,key):
        def fn(cur):
            cur.execute(f"SELECT body FROM {Q} WHERE shortcut_key=%s",(key,))
            row=cur.fetchone()
            return row['body'] if row else None
        return await self.call(fn)

    async def shortcut_set(self,key,body,actor):
        def fn(cur):
            cur.execute(f"""INSERT INTO {Q}(shortcut_key,body,changed_by) VALUES(%s,%s,%s)
                ON CONFLICT(shortcut_key) DO UPDATE SET body=EXCLUDED.body,changed_by=EXCLUDED.changed_by,updated_at=NOW()""",(key,body,actor))
        await self.call(fn)

    async def shortcut_delete(self,key):
        def fn(cur):
            cur.execute(f"DELETE FROM {Q} WHERE shortcut_key=%s RETURNING shortcut_key",(key,))
            return bool(cur.fetchone())
        return await self.call(fn)

    async def broadcast_draft(self,author,body,label):
        def fn(cur):
            cur.execute(f"INSERT INTO {B}(author_id,body,label) VALUES(%s,%s,%s) RETURNING id",(author,body,label))
            draft=cur.fetchone()['id']
            if label:
                cur.execute(f"SELECT COUNT(*) c FROM {U} WHERE NOT is_blocked AND NOT bot_blocked AND NOT marketing_opted_out AND EXISTS(SELECT 1 FROM {L} WHERE user_id={U}.user_id AND LOWER(label)=LOWER(%s))",(label,))
            else:
                cur.execute(f"SELECT COUNT(*) c FROM {U} WHERE NOT is_blocked AND NOT bot_blocked AND NOT marketing_opted_out")
            return draft,cur.fetchone()['c']
        return await self.call(fn)

    async def broadcast_confirm(self,bid,author,confirm):
        def fn(cur):
            cur.execute(f"SELECT * FROM {B} WHERE id=%s FOR UPDATE",(bid,))
            row=cur.fetchone()
            if not row or row['author_id']!=author or row['state']!='draft':
                return None
            if not confirm:
                cur.execute(f"UPDATE {B} SET state='cancelled' WHERE id=%s",(bid,))
                return 0
            cur.execute(f"UPDATE {B} SET state='queued',confirmed_at=NOW() WHERE id=%s",(bid,))
            label=row['label']
            if label:
                cur.execute(f"""SELECT user_id,name FROM {U}
                    WHERE NOT is_blocked AND NOT bot_blocked AND NOT marketing_opted_out
                    AND EXISTS(SELECT 1 FROM {L} WHERE user_id={U}.user_id AND LOWER(label)=LOWER(%s))
                    ORDER BY last_seen ASC,topic_id ASC,user_id ASC""",(label,))
            else:
                cur.execute(f"""SELECT user_id,name FROM {U}
                    WHERE NOT is_blocked AND NOT bot_blocked AND NOT marketing_opted_out
                    ORDER BY last_seen ASC,topic_id ASC,user_id ASC""")
            customers=cur.fetchall()
            for user in customers:
                first=(user['name'] or 'there').split(' ')[0]
                content=f"📢 WishWheel UPDATE\n\n{row['body'].replace('{name}', first)}"
                cur.execute(f"""INSERT INTO {O}(user_id,direction,body,idempotency_key)
                    VALUES(%s,'broadcast',%s,%s) ON CONFLICT (idempotency_key) DO NOTHING""",
                    (user['user_id'],content,f"broadcast:{bid}:{user['user_id']}"))
            return len(customers)
        return await self.call(fn)

    async def old_customers(self,days):
        def fn(cur):
            cur.execute(f"SELECT user_id,topic_id FROM {U} WHERE last_seen<NOW()-(%s * INTERVAL '1 day') LIMIT 50",(days,))
            return cur.fetchall()
        return await self.call(fn)
