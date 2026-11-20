# chatbot

A simple chatbot that talks to a local LLM through [Ollama](https://ollama.com). It comes as a terminal app and as a web app. The web app stores conversations in PostgreSQL and runs on Kubernetes.

See [docs/architecture.md](docs/architecture.md) for diagrams of the components and the request flow.

## Files

| File | What it is |
|---|---|
| `chat.py` | Terminal chat app, which keeps history in memory. Also holds `stream_chat()`, the Ollama client both apps use |
| `server.py` | Web server: the chat page plus the streaming chat API |
| `db.py` | Message storage in PostgreSQL |
| `static/index.html` | The chat page |
| `requirements.txt` | Python dependencies for the web app (`psycopg`) |
| `Dockerfile` | Container image for the web app |
| `k8s/` | Kubernetes manifests (kustomize) and the kind cluster config |
| `Makefile` | Shortcuts: `make help` lists them |

## How conversations are stored

- Each browser chat has a **conversation ID**: a random UUID the page keeps in localStorage.
- Every question and answer is saved in Postgres under that ID, in the `conversations` and `messages` tables.
- For each new question, the server loads the last 50 messages (`MAX_HISTORY`) as context for the model. Any replica can answer any request, because they all share the database.
- A question and its answer are saved together, and only when the reply completes. A failed reply leaves nothing half-saved.
- **New chat** calls `POST /api/reset`, which deletes the conversation and all its messages, and the page starts a new ID. If a reply is still streaming at that moment, it isn't saved.
- Reloading the page brings the conversation back.

## Run on this machine

Start Ollama first with `open -a Ollama`.

```bash
make run      # terminal chat (no database)
```

```bash
make serve    # web app on http://localhost:8099, with Postgres in Docker on 127.0.0.1:5433
```

```bash
make db-stop  # remove the local Postgres and its data
```

`make serve` creates `.venv` and a git-ignored `.env` holding a random database password.

Terminal options: `--model`, `--host` and `--system "..."`. In the terminal chat, `/reset` clears the history, `/help` lists the commands, and `/exit` or Ctrl-D quits.

## Run on Kubernetes (local kind cluster)

```bash
make k8s-up       # create the cluster, build, deploy, wait; then open http://localhost:8090
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
make k8s-down     # delete the cluster, the model and all stored messages
```

The cluster is named `chatbot` (kubectl context `kind-chatbot`, namespace `chatbot`). It holds:

- **chatbot:** a Deployment with 2 replicas that runs as non-root on a read-only filesystem. A NodePort Service (30090) is mapped to `localhost:8090`. A pod reports ready (`/readyz`) only while it can reach the database.
- **postgres:** a StatefulSet with a 1 Gi volume. Its random password is generated once into `k8s/db-secret.env` (git-ignored) and loaded as the `chatbot-db` Secret.
- **ollama:** a Deployment with 1 replica. The model is kept on a 15 Gi volume, and an init container downloads it on first start. Its Service is internal only.

### Memory

Ollama needs up to 6 Gi to run `gemma4:e2b`, so Docker Desktop needs enough memory for it plus the kind node. If other kind clusters are running, pause them with `docker stop <cluster>-control-plane`, or raise the memory under Docker Desktop → Settings → Resources.

### Speed

Inside Docker on a Mac, Ollama runs on the CPU only, without the GPU (Metal). Replies are noticeably slower than with the native Ollama app. On a real cluster, give the Ollama pod a GPU node (`nvidia.com/gpu: 1`).

## API

| Endpoint | Request | Response |
|---|---|---|
| `POST /api/chat` | `{"conversation_id": "<uuid>", "message": "Hi"}` | Newline-delimited JSON: `{"content": "..."}` pieces, then `{"done": true}`, or `{"error": "..."}` |
| `GET /api/history?conversation_id=<uuid>` | | `{"messages": [{"role": "user", "content": "..."}, ...]}` |
| `POST /api/reset` | `{"conversation_id": "<uuid>"}` | `{"deleted": 4}` |
| `GET /healthz` | | `ok` while the process is up |
| `GET /readyz` | | `ok` while the database answers |

The server adds the system prompt from `SYSTEM_PROMPT`.

See [prompt.md](prompt.md) for the project spec.
