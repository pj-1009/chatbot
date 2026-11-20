# Demo

Steps to show the chatbot's features on Kubernetes, with what you should see after each one. For how the parts fit together, see [architecture.md](architecture.md).

You need a browser at http://localhost:8090 and a terminal in the repository folder.

## Setup

1. Start Docker Desktop.
2. Deploy, or check that the cluster is already running:

   ```bash
   make k8s-up
   ```

   The first run downloads the models (several GB). After that it takes about a minute.
3. Define a shortcut for the kubectl commands below:

   ```bash
   k() { kubectl --context kind-chatbot -n chatbot "$@"; }
   ```
4. Optional: send one message in the browser, then click **New chat**. The first reply after the model has been idle is slow, because the model has to load into memory.

If other kind clusters are running, Ollama may not have enough memory. See "Memory" in the [README](../README.md).

## 1. What's running

```bash
make k8s-status
```

Expected: two `chatbot` pods, one `ollama` pod and `postgres-0`, all `Running`, plus one or more `ingest` jobs `Completed`.

## 2. Streaming chat

In the browser, send:

> Explain Kubernetes to a five-year-old in three sentences.

Expected: the reply appears word by word.

## 3. Conversation memory

Send these one after the other:

> My name is Sam and my favourite colour is teal. Reply in one sentence.

> What's my name and favourite colour?

Expected: the second reply says *Sam* and *teal*.

## 4. Messages in the database

```bash
k exec postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "SELECT left(conversation_id::text, 8) AS chat, role, left(content, 50) AS content FROM messages ORDER BY id"'
```

Expected: the messages from step 3, with `user` and `assistant` roles, under one conversation ID.

For an interactive session, run `make k8s-db` (`\q` to quit).

## 5. Reload the page

Reload the browser tab.

Expected: the conversation is still there.

## 6. Both replicas share the history

Send each turn of one conversation to a different pod:

```bash
P1=$(k get pods -l app.kubernetes.io/name=chatbot -o jsonpath='{.items[0].metadata.name}')
P2=$(k get pods -l app.kubernetes.io/name=chatbot -o jsonpath='{.items[1].metadata.name}')
k port-forward pod/$P1 18081:8080 >/dev/null &
k port-forward pod/$P2 18082:8080 >/dev/null &
C=$(uuidgen | tr A-Z a-z)
```

```bash
curl -sN localhost:18081/api/chat -d "{\"conversation_id\":\"$C\",\"message\":\"My name is Alex. Reply in one sentence.\"}"
```

```bash
curl -sN localhost:18082/api/chat -d "{\"conversation_id\":\"$C\",\"message\":\"What is my name? One word.\"}"
```

Expected: pod 2 answers *Alex*.

Stop the port-forwards:

```bash
kill %1 %2
```

## 7. Delete a web pod

Optionally, watch the pods in a second terminal (define `k` there too):

```bash
k get pods -w
```

Delete one web pod while chatting in the browser:

```bash
k delete pod $P1
```

Expected: a replacement pod starts within seconds, and the chat keeps working. A reply the deleted pod was streaming still completes.

## 8. Restart the database

```bash
k delete pod postgres-0
```

```bash
k wait --for=condition=Ready pod/postgres-0 --timeout=120s
```

Reload the browser and ask a follow-up question.

Expected: the conversation is still there and the bot still remembers it. A message sent during the restart may get "database unavailable"; send it again.

## 9. New chat clears the history

Click **New chat**, then run the query from step 4 again.

Expected: the browser conversation's rows are gone. The step 6 conversation is still there.

Delete it too, if `C` is still set:

```bash
curl -s localhost:8090/api/reset -d "{\"conversation_id\":\"$C\"}"
```

## 10. Answers from the documentation (RAG)

Click **New chat**, then send:

> What does the ollama-models volume store, and how big is it?

Expected: *the downloaded models, 15 Gi*, with **Sources: docs/architecture.md** (and possibly others) under the reply.

> And the database volume?

Expected: *1 Gi*. The follow-up is searched together with the previous question.

> What is the capital of Japan? One word.

Expected: *Tokyo*, with no sources line.

List what the knowledge base holds:

```bash
k exec postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "SELECT source, count(*) AS chunks FROM chunks GROUP BY source ORDER BY source"'
```

Optional: add a `.md` file with a made-up fact to `knowledge/`, run `make k8s-up`, and ask about it. Only the new file is ingested.

## 11. Logs

```bash
make k8s-logs
```

Send a message in the browser. Expected: a `POST /api/chat` line, prefixed with the pod that handled it. Ctrl-C to stop.

## Afterwards

Stop the cluster, keeping its data:

```bash
docker stop chatbot-control-plane
```

Start it again:

```bash
docker start chatbot-control-plane
```

Delete everything, including the models and stored chats:

```bash
make k8s-down
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Page doesn't load at :8090 | The cluster isn't running | `docker start chatbot-control-plane`, then `make k8s-status` |
| A reply takes 30+ seconds | The model is loading after being idle | Wait; later replies are faster |
| "can't reach Ollama" | The Ollama pod is restarting or out of memory | `k get pods`, then `k describe pod -l app.kubernetes.io/name=ollama` |
| "database unavailable" for more than a few seconds | Postgres isn't ready | `k get pods`, then `k logs postgres-0` |
| `ollama` pod shows `OOMKilled` | Docker is short on memory | Stop other kind clusters, or give Docker more memory |
