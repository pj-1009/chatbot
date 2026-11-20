# Chatbot — Project Spec

## Goal
Build a simple chatbot that uses a locally running LLM served by **Ollama**.

## LLM setup (done)
- **Runtime:** Ollama 0.35.1, installed as `/Applications/Ollama.app` (CLI at `/usr/local/bin/ollama`)
- **Server:** `http://localhost:11434`. Start it with `open -a Ollama`, or run `ollama serve`
- **Model:** `gemma4:e2b` (4.6 GB, already pulled). It fits this machine (Apple Silicon, 16 GB RAM)
- **Verified:** `POST /api/chat` returns a response

### Example request
```bash
curl -s localhost:11434/api/chat -d '{
  "model": "gemma4:e2b",
  "stream": false,
  "messages": [{"role": "user", "content": "Hello"}]
}'
```

## Chatbot requirements (initial)
- Talk to Ollama's `/api/chat` endpoint
- Keep the conversation history so the bot has context across turns
- Stream responses token by token
- Make the model name and server URL configurable, with defaults `gemma4:e2b` and `http://localhost:11434`

## Decisions
- **Stack:** a Python CLI (`chat.py`) that uses only the standard library (`urllib`), so there's nothing to install
- **Interface:** terminal, with commands `/reset`, `/help` and `/exit`
- **System prompt:** optional, set with `--system "..."`
- **Config:** `--model` and `--host` flags, or the `OLLAMA_MODEL` and `OLLAMA_HOST_URL` environment variables

## Web app and Kubernetes
- **Web app:** `server.py` (standard library `http.server`) serves `static/index.html` and `POST /api/chat`, which streams newline-delimited JSON
- **History in Postgres:** messages are stored in PostgreSQL (`db.py`, driver `psycopg`), keyed by a conversation ID (a UUID the browser picks and keeps in localStorage)
  - All replicas read and write the same history
  - The model gets the last `MAX_HISTORY` (50) messages as context
  - A question and its answer are saved together, only when the reply completes
  - "New chat" calls `POST /api/reset`, which deletes the conversation and its messages, and the browser starts a new ID
  - A reply still streaming during a reset is not saved
  - The chat comes back after a page reload
- **System prompt:** set server-side with `SYSTEM_PROMPT`. Clients can't send `system` messages
- **Kubernetes:** its own kind cluster, `chatbot`, with namespace `chatbot`
  - Ollama is a Deployment with a PersistentVolumeClaim for the models; an init container pulls the model
  - The chatbot is a Deployment with 2 replicas and a NodePort Service at `localhost:8090`
  - Postgres is a StatefulSet with a 1 Gi volume; its random password lives in a generated Secret (`k8s/db-secret.env`, git-ignored)
- **Commands:** `make k8s-up`, `k8s-status`, `k8s-logs` and `k8s-down`

## Open decisions
- Whether to clean up old conversations that were never reset (they're kept forever today)
- User accounts: today anyone who has a conversation ID can read that conversation
- A GPU node for Ollama on a real cluster, and an Ingress with TLS instead of a NodePort
