#!/usr/bin/env python3
"""HTTP server for the chatbot: a web chat page plus a streaming chat API.

Conversations are stored in PostgreSQL (see db.py), so every replica sees the
same history. The browser only keeps a conversation ID.

Endpoints:
  GET  /                                  the chat page
  GET  /healthz                           liveness: the process is up
  GET  /readyz                            readiness: the database answers
  GET  /api/history?conversation_id=ID    {"messages": [{"role", "content"}, ...]}
  POST /api/chat    {"conversation_id": ID, "message": "..."}
                    replies with newline-delimited JSON: first {"sources": [...]}
                    (the knowledge-base chunks used, possibly none), then
                    {"content": "..."} pieces, then {"done": true}, or
                    {"error": "..."} if something failed.
  POST /api/reset   {"conversation_id": ID}  deletes the conversation's messages

A conversation ID is a UUID chosen by the client.

Configuration (environment variables):
  OLLAMA_HOST_URL  Ollama server URL  (default http://localhost:11434)
  OLLAMA_MODEL     model name         (default gemma4:e2b)
  SYSTEM_PROMPT    optional system prompt added to every conversation
  MAX_HISTORY      how many earlier messages to send to the model (default 20)
  EMBED_MODEL      embedding model for RAG (default nomic-embed-text)
  RAG_TOP_K        how many knowledge-base chunks to add to a question (default 4; 0 turns RAG off)
  RAG_MAX_DISTANCE leave out chunks less similar than this (cosine distance, default 0.38)
  RAG_MIN_KEYWORD_SCORE  leave out weaker keyword matches (0 to 1, default 0.15)
  PORT             port to listen on  (default 8080)
  PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE (or DATABASE_URL)  the database
"""

import json
import os
import signal
import socket
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import rag
from chat import DEFAULT_HOST, DEFAULT_MODEL, stream_chat
from db import Store

OLLAMA_HOST = os.environ.get("OLLAMA_HOST_URL", DEFAULT_HOST)
MODEL = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "").strip()
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "20"))
EMBED_MODEL = os.environ.get("EMBED_MODEL", rag.DEFAULT_EMBED_MODEL)
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "4"))
RAG_MAX_DISTANCE = float(os.environ.get("RAG_MAX_DISTANCE", "0.38"))
RAG_MIN_KEYWORD_SCORE = float(os.environ.get("RAG_MIN_KEYWORD_SCORE", "0.15"))
PORT = int(os.environ.get("PORT", "8080"))

MAX_BODY_BYTES = 100_000
MAX_MESSAGE_CHARS = 20_000
INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_bytes()

store = Store()


def conversation_id(value):
    """Return the ID in canonical form, or raise ValueError."""
    if not isinstance(value, str):
        raise ValueError("conversation_id must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ValueError("conversation_id must be a UUID") from None


class Handler(BaseHTTPRequestHandler):
    server_version = "chatbot/3.0"

    # --- routing ---------------------------------------------------------

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/":
            self.send_bytes(200, "text/html; charset=utf-8", INDEX_HTML)
        elif url.path == "/healthz":
            self.send_bytes(200, "text/plain; charset=utf-8", b"ok\n")
        elif url.path == "/readyz":
            try:
                store.ping()
                self.send_bytes(200, "text/plain; charset=utf-8", b"ok\n")
            except Exception as e:
                self.send_json(503, {"error": f"database unavailable: {e}"})
        elif url.path == "/api/history":
            try:
                cid = conversation_id(parse_qs(url.query).get("conversation_id", [None])[0])
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
                return
            self.with_db(lambda: self.send_json(200, {"messages": store.history(cid, MAX_HISTORY)}))
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/api/chat", "/api/reset"):
            self.send_json(404, {"error": "not found"})
            return
        body = self.read_json()
        if body is None:
            return
        try:
            cid = conversation_id(body.get("conversation_id"))
        except ValueError as e:
            self.send_json(400, {"error": str(e)})
            return

        if self.path == "/api/reset":
            self.with_db(lambda: self.send_json(200, {"deleted": store.clear(cid)}))
            return

        text = body.get("message")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
            self.send_json(400, {"error": f"message must be 1 to {MAX_MESSAGE_CHARS} characters"})
            return
        self.chat(cid, text.strip())

    # --- chat ------------------------------------------------------------

    def chat(self, cid, text):
        try:
            store.start(cid)
            history = store.history(cid, MAX_HISTORY)
        except Exception as e:
            self.log_message("database error: %s", e)
            self.send_json(503, {"error": "the database is unavailable, try again shortly"})
            return

        chunks = self.retrieve(history, text)
        system = "\n\n".join(p for p in (SYSTEM_PROMPT, rag.context_prompt(chunks) if chunks else "") if p)
        messages = history + [{"role": "user", "content": text}]
        if system:
            messages.insert(0, {"role": "system", "content": system})

        # Stream the reply. The response has no Content-Length, so the
        # connection closes when the reply ends (HTTP/1.0 style).
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")  # don't let a proxy buffer the stream
        self.end_headers()
        try:
            self.write_line({"sources": [
                {"source": c["source"], "heading": c["heading"],
                 "distance": c["distance"], "keyword_score": c["keyword_score"]}
                for c in chunks
            ]})
        except (BrokenPipeError, ConnectionResetError):
            return

        reply = []
        try:
            for piece in stream_chat(OLLAMA_HOST, MODEL, messages):
                if piece:
                    reply.append(piece)
                    self.write_line({"content": piece})
        except (BrokenPipeError, ConnectionResetError):
            return  # the browser went away: save nothing; leaving the loop also stops Ollama
        except urllib.error.HTTPError as e:
            self.safe_error(f"Ollama returned HTTP {e.code}: {e.read().decode(errors='replace')}")
            return
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            self.safe_error(f"can't reach Ollama at {OLLAMA_HOST} ({reason})")
            return
        except RuntimeError as e:
            self.safe_error(str(e))
            return

        # Save only complete exchanges, so a failed reply leaves no half-turn behind.
        if reply:
            try:
                store.add_exchange(cid, text, "".join(reply))
            except Exception as e:
                self.safe_error(f"the reply couldn't be saved: {e}")
                return
        try:
            self.write_line({"done": True})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def retrieve(self, history, text):
        """The knowledge-base chunks relevant to this question (RAG).

        Returns [] when RAG is off, nothing matches, or the search fails: the
        bot then answers without extra context rather than not at all.
        """
        if RAG_TOP_K <= 0:
            return []
        try:
            query = rag.search_text(history, text)
            vector = rag.embed_query(OLLAMA_HOST, EMBED_MODEL, query)
            return store.search(vector, query, RAG_TOP_K, RAG_MAX_DISTANCE, RAG_MIN_KEYWORD_SCORE)
        except Exception as e:
            self.log_message("knowledge-base search failed, answering without it: %s", e)
            return []

    # --- helpers ---------------------------------------------------------

    def read_json(self):
        """Read and parse a JSON object body, or send a 4xx and return None."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_json(400 if length <= 0 else 413, {"error": "missing or too large body"})
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError) as e:
            self.send_json(400, {"error": f"invalid JSON: {e}"})
            return None
        if not isinstance(body, dict):
            self.send_json(400, {"error": "body must be a JSON object"})
            return None
        return body

    def with_db(self, action):
        try:
            action()
        except Exception as e:
            self.log_message("database error: %s", e)
            self.send_json(503, {"error": "the database is unavailable, try again shortly"})

    def write_line(self, obj):
        self.wfile.write(json.dumps(obj).encode() + b"\n")
        self.wfile.flush()

    def safe_error(self, message):
        self.log_message("chat error: %s", message)
        try:
            self.write_line({"error": message})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_bytes(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj):
        self.send_bytes(status, "application/json", json.dumps(obj).encode())


def stop(signum, frame):
    raise KeyboardInterrupt


def main():
    store.open()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    # On shutdown, server_close() waits for requests in progress, so replies
    # that are still streaming get to finish.
    server.daemon_threads = False
    # Kubernetes sends SIGTERM to stop a pod: stop taking new requests, let
    # the current ones finish, then exit.
    signal.signal(signal.SIGTERM, stop)
    print(f"chatbot listening on :{PORT}, model {MODEL} at {OLLAMA_HOST}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
