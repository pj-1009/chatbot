"""Storage in PostgreSQL, shared by every replica of the web app.

Holds the chat messages, and the knowledge base for RAG: document chunks
with their embeddings, searched with the pgvector extension.

Connection settings come from the standard libpq environment variables:
PGHOST, PGPORT, PGUSER, PGPASSWORD and PGDATABASE (or DATABASE_URL).
"""

import os
import time

from psycopg_pool import ConnectionPool

# Must match the embedding model's output size (nomic-embed-text: 768).
EMBED_DIM = 768

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    source       TEXT        PRIMARY KEY,   -- file path, e.g. docs/architecture.md
    content_hash TEXT        NOT NULL,      -- to skip files that haven't changed
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chunks (
    id        BIGSERIAL PRIMARY KEY,
    source    TEXT NOT NULL REFERENCES documents (source) ON DELETE CASCADE,
    heading   TEXT NOT NULL,                -- the section the chunk comes from
    content   TEXT NOT NULL,
    embedding VECTOR({EMBED_DIM}) NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
-- Words of each chunk, for keyword search next to the vector search.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS words TSVECTOR
    GENERATED ALWAYS AS (to_tsvector('english', heading || ' ' || content)) STORED;
CREATE INDEX IF NOT EXISTS chunks_words_idx ON chunks USING gin (words);

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
        # check= tests each connection before handing it out and replaces dead
        # ones, so the app recovers by itself after the database restarts.
        self.pool = ConnectionPool(conninfo, min_size=1, max_size=10, open=False,
                                   check=ConnectionPool.check_connection)

    def open(self, wait_seconds=120):
        """Connect (retrying while the database starts up) and create the tables."""
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

    # --- knowledge base (RAG) -------------------------------------------

    def document_hashes(self):
        """{source: content_hash} for every ingested document."""
        with self.pool.connection() as conn:
            return dict(conn.execute("SELECT source, content_hash FROM documents").fetchall())

    def replace_document(self, source, content_hash, chunks):
        """Store a document's chunks, replacing any older version, in one transaction.

        `chunks` is a list of (heading, content, embedding) tuples.
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM documents WHERE source = %s", (source,))
                cur.execute("INSERT INTO documents (source, content_hash) VALUES (%s, %s)",
                            (source, content_hash))
                cur.executemany(
                    "INSERT INTO chunks (source, heading, content, embedding)"
                    " VALUES (%s, %s, %s, %s::vector)",
                    [(source, h, c, to_vector(e)) for h, c, e in chunks],
                )

    def delete_documents(self, sources):
        """Remove documents (and their chunks). Returns how many were removed."""
        if not sources:
            return 0
        with self.pool.connection() as conn:
            return conn.execute("DELETE FROM documents WHERE source = ANY(%s)",
                                (list(sources),)).rowcount

    def search(self, embedding, text, limit, max_distance, min_keyword_score,
               candidates=20):
        """The chunks most relevant to a question, best first (hybrid search).

        Runs two searches and merges them:
        - by meaning: chunks whose embedding is close to the question's.
          Distance is cosine distance (0 = same meaning, 1 = unrelated);
          chunks further than `max_distance` are left out.
        - by keywords: Postgres full-text search for the question's words.
          This finds exact names (like "ollama-models") that embeddings miss.
          Matches scoring below `min_keyword_score` (0 to 1), or containing
          no more than half of the question's words, are left out. (Without
          the second rule, "What is the capital of Japan? One word." matches
          any chunk that says "one word".)

        The two ranked lists are merged with reciprocal rank fusion: a chunk
        scores 1/(60 + its position) in each list it appears in. A question
        unrelated to the documents passes neither filter and gets no chunks.
        """
        with self.pool.connection() as conn:
            by_meaning = conn.execute(
                """SELECT id, distance FROM (
                       SELECT id, embedding <=> %s::vector AS distance
                       FROM chunks ORDER BY distance LIMIT %s
                   ) nearest WHERE distance <= %s ORDER BY distance""",
                (to_vector(embedding), candidates, max_distance),
            ).fetchall()
            # websearch_to_tsquery requires every word; swapping AND for OR
            # finds chunks with any of them, ranked by how many and how close.
            by_keywords = conn.execute(
                """WITH q AS (
                       SELECT replace(websearch_to_tsquery('english', %(text)s)::text, ' & ', ' | ')::tsquery AS query,
                              tsvector_to_array(to_tsvector('english', %(text)s)) AS words
                   )
                   SELECT id, score FROM (
                       SELECT id, ts_rank_cd(c.words, q.query, 32) AS score,
                              (SELECT count(*) FROM unnest(q.words) w
                               WHERE w = ANY(tsvector_to_array(c.words)))::float
                                  / greatest(cardinality(q.words), 1) AS coverage
                       FROM chunks c, q WHERE c.words @@ q.query
                   ) matches WHERE score >= %(min_score)s AND coverage > 0.5
                   ORDER BY score DESC LIMIT %(limit)s""",
                {"text": text, "min_score": min_keyword_score, "limit": candidates},
            ).fetchall()

            fused = {}
            for ranked in (by_meaning, by_keywords):
                for position, (chunk_id, _) in enumerate(ranked):
                    fused[chunk_id] = fused.get(chunk_id, 0) + 1 / (60 + position)
            best = sorted(fused, key=fused.get, reverse=True)[:limit]
            if not best:
                return []

            distance = dict(by_meaning)
            keyword = dict(by_keywords)
            rows = {r[0]: r[1:] for r in conn.execute(
                "SELECT id, source, heading, content FROM chunks WHERE id = ANY(%s)", (best,)
            ).fetchall()}
        return [{"source": rows[i][0], "heading": rows[i][1], "content": rows[i][2],
                 "distance": round(distance[i], 3) if i in distance else None,
                 "keyword_score": round(keyword[i], 3) if i in keyword else None}
                for i in best]

    def ping(self):
        with self.pool.connection(timeout=3) as conn:
            conn.execute("SELECT 1")


def to_vector(values):
    """Format a list of floats as a pgvector literal, e.g. '[0.1,0.2]'."""
    if len(values) != EMBED_DIM:
        raise ValueError(f"expected an embedding of {EMBED_DIM} numbers, got {len(values)}")
    return "[" + ",".join(repr(float(v)) for v in values) + "]"
