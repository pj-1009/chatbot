# chatbot

A simple chatbot that talks to a local LLM through [Ollama](https://ollama.com). It comes as a terminal app and as a web app. The web app:

- stores conversations in PostgreSQL;
- answers questions about this project from its own documentation, using RAG (retrieval-augmented generation);
- runs on Kubernetes.

See [docs/architecture.md](docs/architecture.md) for diagrams of the components and the request flow.

## Files

| File | What it is |
|---|---|
| `chat.py` | Terminal chat app, which keeps history in memory. Also holds `stream_chat()`, the Ollama client both apps use |
| `server.py` | Web server: the chat page plus the streaming chat API |
| `db.py` | Storage in PostgreSQL: conversations, and the knowledge base with pgvector search |
| `rag.py` | RAG helpers: splitting documents into chunks, embeddings, building the prompt |
| `ingest.py` | Loads documents into the knowledge base |
| `knowledge/` | Put your own `.md` or `.txt` files here for the chatbot to answer from |
| `static/index.html` | The chat page |
| `requirements.txt` | Python dependencies for the web app (`psycopg`) |
| `Dockerfile` | Container image for the web app and the ingest job |
| `k8s/` | Kubernetes manifests (kustomize) and the kind cluster config |
| `Makefile` | Shortcuts: `make help` lists them |

## How conversations are stored

- Each browser chat has a **conversation ID**: a random UUID the page keeps in localStorage.
- Every question and answer is saved in Postgres under that ID, in the `conversations` and `messages` tables.
- For each new question, the server loads the last 20 messages (`MAX_HISTORY`) as context for the model. Any replica can answer any request, because they all share the database.
- A question and its answer are saved together, and only when the reply completes. A failed reply leaves nothing half-saved.
- **New chat** calls `POST /api/reset`, which deletes the conversation and all its messages, and the page starts a new ID. If a reply is still streaming at that moment, it isn't saved.
- Reloading the page brings the conversation back.

## How the bot answers from documents (RAG)

The knowledge base is built from `README.md`, `prompt.md`, `docs/` and `knowledge/`.

1. **Loading** (`ingest.py`):
   - Each file is split into chunks of about 700 characters, following its headings.
   - Each chunk is turned into an embedding (768 numbers that capture its meaning) by Ollama's `nomic-embed-text` model.
   - The chunks and embeddings go into the `documents` and `chunks` tables, using the [pgvector](https://github.com/pgvector/pgvector) extension.
   - Unchanged files are skipped on later runs.
2. **Searching:** for each question, the server runs two searches and merges the results:
   - **by meaning:** the chunks whose embeddings are closest to the question's (cosine distance at most `RAG_MAX_DISTANCE`, 0.38).
   - **by keywords:** Postgres full-text search, which catches exact names like `ollama-models` that embeddings miss. A match must cover more than half of the question's words.

   For follow-up questions ("and what about…?", "how do I stop it?"), the previous question is searched along with them.
3. **Answering:** up to 4 chunks (`RAG_TOP_K`) are added to the system prompt, with an instruction to use them when relevant and name the source. The page shows the documents used under each reply.

A question unrelated to the documents (like "write a poem") matches nothing, so the model answers normally. If the knowledge base can't be searched at all, the bot still answers, without it.

## Run on this machine

Start Ollama first with `open -a Ollama`. Pull the embedding model once:

```bash
ollama pull nomic-embed-text
```

Then:

```bash
make run      # terminal chat (no database, no RAG)
```

```bash
make ingest   # load the knowledge base into a local Postgres (Docker, 127.0.0.1:5433)
```

```bash
make serve    # web app on http://localhost:8099, using that Postgres
```

```bash
make db-stop  # remove the local Postgres and its data
```

`make serve` and `make ingest` create `.venv` and a git-ignored `.env` holding a random database password.

Terminal options: `--model`, `--host` and `--system "..."`. In the terminal chat, `/reset` clears the history, `/help` lists the commands, and `/exit` or Ctrl-D quits.

## Run on Kubernetes (local kind cluster)

```bash
make k8s-up       # create the cluster, build, deploy, load the knowledge base; then open http://localhost:8090
```

```bash
make k8s-ingest   # reload the knowledge base (after make k8s-up has rebuilt the image)
```

```bash
make k8s-status   # pods, services, volumes
```

```bash
make k8s-logs     # follow the logs
```

```bash
make k8s-db       # psql on the cluster's database
```

```bash
make k8s-down     # delete the cluster, the models and all stored data
```

The cluster is named `chatbot` (kubectl context `kind-chatbot`, namespace `chatbot`). It holds:

- **chatbot:** a Deployment with 2 replicas that runs as non-root on a read-only filesystem.
  - A NodePort Service (30090) is mapped to `localhost:8090`.
  - A pod reports ready (`/readyz`) only while it can reach the database.
  - When a pod is stopped, it finishes replies in progress before exiting.
- **postgres:** a StatefulSet using the `pgvector/pgvector:pg17` image, with a 1 Gi volume.
  - Its random password is generated once into `k8s/db-secret.env` (git-ignored) and loaded as the `chatbot-db` Secret.
- **ollama:** a Deployment with 1 replica, running `gemma4:e2b` for chat and `nomic-embed-text` for embeddings.
  - Both models are kept on a 15 Gi volume, and an init container downloads them on first start.
  - Its Service is internal only.
- **ingest:** a suspended CronJob, used as a template. `make k8s-ingest` starts a one-off Job from it to load the knowledge base.
  - The documents are baked into the chatbot image, so after editing them, run `make k8s-up`, which rebuilds the image and re-ingests.

### Adding your own documents

Put `.md` or `.txt` files in `knowledge/`, then run `make k8s-up` (cluster) or `make ingest` (local). Files you delete from there are removed from the knowledge base on the next run.

### Memory

Ollama needs up to 6 Gi with both models loaded. The cluster uses about 6.3 GB in total, which is most of Docker Desktop's default 7.7 GB. If other kind clusters are running, pause them with `docker stop <cluster>-control-plane`, or raise the memory under Docker Desktop → Settings → Resources.

### Speed

Inside Docker on a Mac, Ollama runs on the CPU only, without the GPU (Metal). Replies are noticeably slower than with the native Ollama app. On a real cluster, give the Ollama pod a GPU node (`nvidia.com/gpu: 1`).

## API

| Endpoint | Request | Response |
|---|---|---|
| `POST /api/chat` | `{"conversation_id": "<uuid>", "message": "Hi"}` | Newline-delimited JSON: `{"sources": [...]}` first, then `{"content": "..."}` pieces, then `{"done": true}`, or `{"error": "..."}` |
| `GET /api/history?conversation_id=<uuid>` | | `{"messages": [{"role": "user", "content": "..."}, ...]}` |
| `POST /api/reset` | `{"conversation_id": "<uuid>"}` | `{"deleted": 4}` |
| `GET /healthz` | | `ok` while the process is up |
| `GET /readyz` | | `ok` while the database answers |

Each entry in `sources` has `source` (the file), `heading` (its section), `distance` (meaning match, lower is closer) and `keyword_score` (keyword match). Either score is `null` if that search didn't find the chunk.

### Settings

| Variable | Default | What it does |
|---|---|---|
| `OLLAMA_HOST_URL` | `http://localhost:11434` | Ollama server |
| `OLLAMA_MODEL` | `gemma4:e2b` | Chat model |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model. Changing it means re-ingesting with `--force`, and possibly changing `EMBED_DIM` in `db.py` |
| `SYSTEM_PROMPT` | (none) | Added to every conversation |
| `MAX_HISTORY` | `20` | Earlier messages sent to the model |
| `RAG_TOP_K` | `4` | Knowledge-base chunks per question; `0` turns RAG off |
| `RAG_MAX_DISTANCE` | `0.38` | Meaning matches further away than this are ignored |
| `RAG_MIN_KEYWORD_SCORE` | `0.15` | Keyword matches scoring lower are ignored |

See [prompt.md](prompt.md) for the project spec.
