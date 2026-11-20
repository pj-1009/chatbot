#!/usr/bin/env python3
"""A simple terminal chatbot backed by a local Ollama model."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemma4:e2b"
DEFAULT_HOST = "http://localhost:11434"

HELP = """Commands:
  /reset   clear the conversation history
  /help    show this help
  /exit    quit (Ctrl-D also works)"""


def stream_chat(host, model, messages, timeout=300):
    """Send the conversation to Ollama and yield reply text as it streams in.

    `timeout` is how long to wait for each piece of the reply, in seconds.
    """
    body = json.dumps({"model": model, "messages": messages, "stream": True}).encode()
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in resp:
            if not line.strip():
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise RuntimeError(chunk["error"])
            yield chunk.get("message", {}).get("content", "")
            if chunk.get("done"):
                break


def initial_history(system):
    return [{"role": "system", "content": system}] if system else []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL),
                        help=f"model name (env OLLAMA_MODEL, default {DEFAULT_MODEL})")
    parser.add_argument("--host", default=os.environ.get("OLLAMA_HOST_URL", DEFAULT_HOST),
                        help=f"Ollama server URL (env OLLAMA_HOST_URL, default {DEFAULT_HOST})")
    parser.add_argument("--system", default=None, help="optional system prompt")
    args = parser.parse_args()

    messages = initial_history(args.system)
    interactive = sys.stdin.isatty()
    if interactive:
        print(f"Chatting with {args.model}. Type /help for commands.\n")

    while True:
        try:
            user = input("you> " if interactive else "")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        user = user.strip()
        if not user:
            continue
        if user in ("/exit", "/quit"):
            break
        if user == "/help":
            print(HELP)
            continue
        if user == "/reset":
            messages = initial_history(args.system)
            print("(history cleared)")
            continue

        messages.append({"role": "user", "content": user})
        print("bot> ", end="", flush=True)
        reply = []
        try:
            for piece in stream_chat(args.host, args.model, messages):
                print(piece, end="", flush=True)
                reply.append(piece)
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            print(f"\n[error] HTTP {e.code}: {detail}")
            messages.pop()
            continue
        except urllib.error.URLError as e:
            print(f"\n[error] can't reach Ollama at {args.host} ({e.reason}). "
                  "Is it running? Try: open -a Ollama")
            messages.pop()
            continue
        except RuntimeError as e:
            print(f"\n[error] {e}")
            messages.pop()
            continue
        print("\n")

        if reply:
            messages.append({"role": "assistant", "content": "".join(reply)})
        else:
            messages.pop()


if __name__ == "__main__":
    main()
