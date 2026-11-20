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
  - The model gets the last `MAX_HISTORY` (20) messages as context
  - A question and its answer are saved together, only when the reply completes
  - "New chat" calls `POST /api/reset`, which deletes the conversation and its messages, and the browser starts a new ID
  - A reply still streaming during a reset is not saved
  - The chat comes back after a page reload
- **System prompt:** set server-side with `SYSTEM_PROMPT`. Clients can't send `system` messages
- **Kubernetes:** its own kind cluster, `chatbot`, with namespace `chatbot`
  - Ollama is a Deployment with a PersistentVolumeClaim for the models; an init container pulls the model
  - The chatbot is a Deployment with 2 replicas and a NodePort Service at `localhost:8090`
  - Postgres is a StatefulSet with a 1 Gi volume; its random password lives in a generated Secret (`k8s/db-secret.env`, git-ignored)
- **Commands:** `make k8s-up`, `k8s-ingest`, `k8s-status`, `k8s-logs`, `k8s-db` and `k8s-down`

## RAG (answering from the project's documents)
- **Knowledge base:** `README.md`, `prompt.md`, `docs/` and `knowledge/` (for the user's own files), split into ~700-character chunks by heading
- **Embeddings:** Ollama's `nomic-embed-text` (768 dimensions), stored in Postgres with **pgvector**. Chosen over a separate vector database because Postgres is already there
- **Loading:** `ingest.py`, which skips unchanged files and removes deleted ones
  - Locally: `make ingest`
  - On the cluster: a Job started from a suspended CronJob (`make k8s-ingest`, also run by `make k8s-up`)
- **Search:** hybrid. Vector search (cosine distance ≤ 0.38) and Postgres full-text search (must cover over half the question's words) are merged with reciprocal rank fusion, and the top 4 chunks are used
  - Follow-up questions also search with the previous question
  - The cut-offs were tuned on real questions: unrelated ones ("capital of Japan", "a poem") get no excerpts
- **Answering:** the chunks go in the system prompt, with an instruction to use them only when relevant and name the source. The page shows the sources under each reply
- **Failure handling:** if the search fails, the bot answers without RAG rather than failing
- **Ollama context:** 4096 tokens. 8192 pushed Ollama to its 6 Gi memory limit with both models loaded

## Open decisions
- Whether to clean up old conversations that were never reset (they're kept forever today)
- User accounts: today anyone who has a conversation ID can read that conversation
- A GPU node for Ollama on a real cluster, and an Ingress with TLS instead of a NodePort
- RAG: PDF support, an upload endpoint instead of baking documents into the image, and saving each reply's sources so they reappear after a page reload
