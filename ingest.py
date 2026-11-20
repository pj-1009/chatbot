#!/usr/bin/env python3
"""Load documents into the chatbot's knowledge base.

Splits each Markdown or text file into chunks, gets an embedding for each
chunk from Ollama, and stores them in Postgres. Files that haven't changed
since the last run are skipped.

Usage:
  python ingest.py README.md docs knowledge          # files and/or folders
  python ingest.py --prune README.md docs knowledge  # also remove documents
                                                     # that are no longer listed
  python ingest.py --force ...                       # re-embed everything

Configuration: OLLAMA_HOST_URL, EMBED_MODEL and the PG* database variables,
as for server.py.
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

from chat import DEFAULT_HOST
from db import Store
from rag import DEFAULT_EMBED_MODEL, chunk_markdown, embed_documents

EXTENSIONS = {".md", ".markdown", ".txt"}


def find_files(paths):
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in EXTENSIONS)
        elif p.is_file():
            files.append(p)
        else:
            sys.exit(f"not found: {p}")
    return files


def title_of(text, path):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.name


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+", help="files or folders to load")
    parser.add_argument("--prune", action="store_true",
                        help="remove stored documents that aren't among these paths any more")
    parser.add_argument("--force", action="store_true", help="re-embed files even if unchanged")
    args = parser.parse_args()

    host = os.environ.get("OLLAMA_HOST_URL", DEFAULT_HOST)
    model = os.environ.get("EMBED_MODEL", DEFAULT_EMBED_MODEL)
    store = Store()
    store.open()
    try:
        stored = store.document_hashes()
        seen = set()
        changed = 0
        for path in find_files(args.paths):
            source = path.as_posix()
            seen.add(source)
            text = path.read_text(encoding="utf-8", errors="replace")
            digest = hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()
            if not args.force and stored.get(source) == digest:
                print(f"unchanged  {source}")
                continue
            chunks = chunk_markdown(text, title_of(text, path))
            # Embed the heading together with the text, so a chunk can be found
            # by what its section is about even if the words don't repeat it.
            vectors = embed_documents(host, model, [f"{h}\n\n{c}" for h, c in chunks])
            store.replace_document(source, digest,
                                   [(h, c, v) for (h, c), v in zip(chunks, vectors)])
            changed += 1
            print(f"loaded     {source}  ({len(chunks)} chunks)")
        removed = store.delete_documents(set(stored) - seen) if args.prune else 0
        print(f"done: {changed} loaded, {len(seen) - changed} unchanged, {removed} removed")
    finally:
        store.close()


if __name__ == "__main__":
    main()
