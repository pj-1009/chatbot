"""Message storage in PostgreSQL, shared by every replica of the web app.

Connection settings come from the standard libpq environment variables:
PGHOST, PGPORT, PGUSER, PGPASSWORD and PGDATABASE (or DATABASE_URL).
"""

import os
import time

from psycopg_pool import ConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id         UUID        PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID        NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages (conversation_id, id);
"""

# Any fixed number: replicas starting together take this lock in turn, so
# only one at a time runs CREATE TABLE.
SCHEMA_LOCK_ID = 726_337


class Store:
    def __init__(self, conninfo=None):
        conninfo = conninfo if conninfo is not None else os.environ.get("DATABASE_URL", "")
        self.pool = ConnectionPool(conninfo, min_size=1, max_size=10, open=False)

    def open(self, wait_seconds=120):
        """Connect (retrying while the database starts up) and create the table."""
        self.pool.open()
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                with self.pool.connection(timeout=5) as conn:
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
                    conn.execute(SCHEMA)
                return
            except Exception as e:
                if time.monotonic() > deadline:
                    raise
                print(f"waiting for the database: {e}", flush=True)
                time.sleep(2)

    def close(self):
        self.pool.close()

    def start(self, conversation_id):
        """Make sure the conversation exists."""
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO conversations (id) VALUES (%s) ON CONFLICT DO NOTHING",
                (conversation_id,),
            )

    def history(self, conversation_id, limit):
        """The last `limit` messages of a conversation, oldest first."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """SELECT role, content FROM (
                       SELECT id, role, content FROM messages
                       WHERE conversation_id = %s ORDER BY id DESC LIMIT %s
                   ) recent ORDER BY id""",
                (conversation_id, limit),
            ).fetchall()
        return [{"role": r, "content": c} for r, c in rows]

    def add_exchange(self, conversation_id, user_text, reply_text):
        """Save a question and its answer together, in one transaction.

        Saves nothing if the conversation was reset while the reply was being
        generated, so a late reply can't bring a cleared conversation back.
        Returns whether it saved.
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                # Lock the conversation row so a concurrent reset waits for us, or we for it.
                cur.execute("SELECT 1 FROM conversations WHERE id = %s FOR UPDATE",
                            (conversation_id,))
                if cur.fetchone() is None:
                    return False
                cur.executemany(
                    "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
                    [(conversation_id, "user", user_text),
                     (conversation_id, "assistant", reply_text)],
                )
                return True

    def clear(self, conversation_id):
        """Delete a conversation and its messages. Returns how many messages were removed."""
        with self.pool.connection() as conn:
            count = conn.execute(
                "SELECT count(*) FROM messages WHERE conversation_id = %s", (conversation_id,)
            ).fetchone()[0]
            conn.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))
            return count

    def ping(self):
        with self.pool.connection(timeout=3) as conn:
            conn.execute("SELECT 1")
