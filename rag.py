"""Retrieval-augmented generation (RAG): splitting documents into chunks,
turning text into embeddings with Ollama, and building the model's context
from the chunks that match a question.
"""

import json
import re
import urllib.request

DEFAULT_EMBED_MODEL = "nomic-embed-text"

# nomic-embed-text expects these prefixes: documents and questions are
# embedded slightly differently, which makes matching more accurate.
DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

CHUNK_CHARS = 700       # target chunk size (about 120 words)
EMBED_BATCH = 16        # chunks per embedding request


def embed(host, model, texts, timeout=120):
    """Return one embedding (a list of floats) per text, using Ollama's /api/embed."""
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        body = json.dumps({"model": model, "input": texts[i:i + EMBED_BATCH]}).encode()
        req = urllib.request.Request(f"{host.rstrip('/')}/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        if "error" in data:
            raise RuntimeError(data["error"])
        out.extend(data["embeddings"])
    return out


def embed_documents(host, model, texts):
    return embed(host, model, [DOC_PREFIX + t for t in texts])


def embed_query(host, model, text):
    return embed(host, model, [QUERY_PREFIX + text])[0]


def chunk_markdown(text, title):
    """Split a Markdown (or plain text) document into chunks.

    Chunks follow the document's sections: a chunk never spans two headings,
    and long sections are split at paragraph breaks. Each chunk records the
    heading path it came from, e.g. "Demo walkthrough > 6. Both copies share".

    Returns a list of (heading, content) tuples.
    """
    chunks = []
    headings = []           # the current heading path, one entry per level
    paragraphs = []         # paragraphs of the current section
    in_code = False
    current = []            # lines of the current paragraph

    def heading_path():
        return " > ".join(h for h in headings if h) or title

    def end_paragraph():
        if current:
            paragraphs.append("\n".join(current).strip())
            current.clear()

    def end_section():
        end_paragraph()
        chunks.extend((heading_path(), c) for c in pack(paragraphs))
        paragraphs.clear()

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            current.append(line)
            continue
        m = None if in_code else re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            end_section()
            level = len(m.group(1))
            headings[level - 1:] = [m.group(2).strip()]
            continue
        if not in_code and not line.strip():
            end_paragraph()
        else:
            current.append(line)
    end_section()
    return [(h, c) for h, c in chunks if c.strip()]


def pack(paragraphs):
    """Join paragraphs into chunks of about CHUNK_CHARS characters."""
    out, buf = [], ""
    for p in paragraphs:
        if not p:
            continue
        if buf and len(buf) + len(p) + 2 > CHUNK_CHARS:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{p}" if buf else p
        # A single paragraph far over the limit is split by lines.
        while len(buf) > CHUNK_CHARS * 2:
            cut = buf.rfind("\n", 0, CHUNK_CHARS) + 1 or CHUNK_CHARS
            out.append(buf[:cut].rstrip())
            buf = buf[cut:]
    if buf:
        out.append(buf)
    return out


FOLLOW_UP_START = re.compile(r"^\s*(and|but|or|also|so|then|what about|how about)\b", re.I)
FOLLOW_UP_WORDS = re.compile(r"\b(it|its|that|this|these|those|they|them|their|there)\b", re.I)


def looks_like_follow_up(question):
    """Whether a question probably refers back to the previous one."""
    return (bool(FOLLOW_UP_START.search(question) or FOLLOW_UP_WORDS.search(question))
            or len(question.split()) <= 4)


def search_text(history, question):
    """The text to search the knowledge base with.

    A follow-up like "and how do I stop it?" doesn't say what "it" is, so
    for follow-ups the user's previous message is searched together with
    the question. A new question is searched on its own, so a change of
    topic doesn't drag in documents about the previous one.
    """
    previous = [m["content"] for m in history if m["role"] == "user"][-1:]
    if previous and looks_like_follow_up(question):
        return f"{previous[0]}\n{question}"
    return question


def context_prompt(chunks):
    """The instructions and retrieved chunks, for the system prompt."""
    parts = [
        "Below are excerpts from this chatbot project's documentation, found by "
        "searching for the user's question. They may or may not be relevant. "
        "If they help answer the question, use them and mention which document "
        "you used. If they aren't relevant, ignore them and answer from your "
        "own knowledge as usual. Only when the question is specifically about "
        "this project and the excerpts don't cover it, say you don't know "
        "rather than guessing.",
    ]
    for i, c in enumerate(chunks, 1):
        parts.append(f"[{i}] From {c['source']} ({c['heading']}):\n{c['content']}")
    return "\n\n".join(parts)
