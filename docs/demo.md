# Demo walkthrough

A 15–20 minute demo of the chatbot on Kubernetes. Each step has what to do, what to say, and what you should see.

Use two windows: a **browser** at http://localhost:8090 and a **terminal** in the `chatbot` folder. For background on the parts, see [architecture.md](architecture.md).

## Before you start

1. Start Docker Desktop.
2. Deploy, or check that the cluster is already running:

   ```bash
   make k8s-up
   ```

   The first run downloads the model (several GB). After that it takes about a minute.
3. Define a shortcut for this demo's kubectl commands, in the terminal you'll use:

   ```bash
   k() { kubectl --context kind-chatbot -n chatbot "$@"; }
   ```
4. Send one message in the browser before the audience arrives, then click **New chat**. The first reply after a while loads the model into memory and is slow. Later replies are faster.

> **Memory:** if other kind clusters are running, Ollama may not have enough memory. See "Memory" in the [README](../README.md).

---

## 1. What's running (1 min)

```bash
make k8s-status
```

You should see four pods `Running`, plus one or more finished ingest jobs:

```
chatbot-…-xxxxx    1/1   Running     ← web app, copy 1
chatbot-…-yyyyy    1/1   Running     ← web app, copy 2
ollama-…           1/1   Running     ← the AI models
postgres-0         1/1   Running     ← the database
ingest-…           0/1   Completed   ← loaded the knowledge base, then exited
```

**Say:** "Four pieces. Two copies of the web app, a database for conversations and documents, and Ollama running the language models. The ingest job loaded the project's documentation into the database, so the bot can answer questions about it. Only the web app is reachable from outside, at port 8090. The database and the models are reachable only inside the cluster."

## 2. Chat, with streaming (1 min)

In the browser, type:

> Explain Kubernetes to a five-year-old in three sentences.

**Point out:** the answer appears word by word. Ollama sends each piece as it's generated, the server passes it straight on, and the page adds it to the screen.

## 3. The bot remembers (1 min)

Type, one after the other:

> My name is Sam and my favourite colour is teal. Reply in one sentence.

> What's my name and favourite colour?

It should answer *Sam* and *teal*.

**Say:** "The model itself has no memory: every request starts from zero. The server loads this conversation's earlier messages from the database and sends them along with each question."

## 4. Look inside the database (1 min)

```bash
k exec postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "SELECT left(conversation_id::text, 8) AS chat, role, left(content, 50) AS content FROM messages ORDER BY id"'
```

You should see your messages from step 3, with `user` and `assistant` roles, all under one conversation ID.

`make k8s-db` opens an interactive `psql` session instead, if you want to explore. Type `\q` to leave.

## 5. Reload the page (30 s)

Press **Cmd-R** in the browser. The conversation comes back.

**Say:** "The browser only remembers its conversation ID. On load it asks the server for that conversation's history."

## 6. Both copies share the same data (2 min)

To prove it, send each turn of one conversation to a *different* pod directly.

```bash
P1=$(k get pods -l app.kubernetes.io/name=chatbot -o jsonpath='{.items[0].metadata.name}')
P2=$(k get pods -l app.kubernetes.io/name=chatbot -o jsonpath='{.items[1].metadata.name}')
k port-forward pod/$P1 18081:8080 >/dev/null &
k port-forward pod/$P2 18082:8080 >/dev/null &
C=$(uuidgen | tr A-Z a-z)
```

Turn 1 goes to pod 1:

```bash
curl -sN localhost:18081/api/chat -d "{\"conversation_id\":\"$C\",\"message\":\"My name is Alex. Reply in one sentence.\"}"
```

Turn 2 goes to pod 2:

```bash
curl -sN localhost:18082/api/chat -d "{\"conversation_id\":\"$C\",\"message\":\"What is my name? One word.\"}"
```

Pod 2 answers **Alex**, although pod 1 handled turn 1.

**Say:** "Neither copy keeps anything in its own memory. They both read and write the same database, so it doesn't matter which one answers."

Stop the port-forwards:

```bash
kill %1 %2
```

## 7. Self-healing: delete a web pod (2 min)

Optionally, in a second terminal, watch the pods (define the `k` shortcut there too):

```bash
k get pods -w
```

Delete one copy of the web app:

```bash
k delete pod $P1
```

Keep chatting in the browser while this happens. Nothing breaks.

**Point out:**
- Kubernetes starts a replacement pod within seconds.
- The old pod stops taking new requests but finishes any reply it was streaming.
- The other copy handles everything in the meantime.

## 8. The database restarts, and nothing is lost (2 min)

```bash
k delete pod postgres-0
```

```bash
k wait --for=condition=Ready pod/postgres-0 --timeout=120s
```

Reload the browser. The conversation is still there. Ask a follow-up question; the bot still remembers.

**Say:** "The database pod was destroyed and recreated, but its data lives on a persistent volume, so nothing was lost. The web app noticed its old database connections were dead and opened new ones."

> A request sent in the few seconds while the database restarts may show "database unavailable". That's expected; try again. If the database stays down for longer (about 15 seconds), Kubernetes also marks the web pods as not ready until it's back.

## 9. New chat clears the history (1 min)

Click **New chat** in the browser, then run the query from step 4 again. The rows from the browser conversation are gone. The `Alex` conversation from step 6 is still there, because it's a different conversation.

**Say:** "New chat deletes this conversation and its messages from the database, and the browser starts a fresh conversation ID."

Clean up the step 6 conversation, if the `C` variable is still set in your terminal:

```bash
curl -s localhost:8090/api/reset -d "{\"conversation_id\":\"$C\"}"
```

## 10. Answering from the documentation: RAG (3 min)

Click **New chat**, then ask a question only this project's docs can answer:

> What does the ollama-models volume store, and how big is it?

It answers *the downloaded models, 15 Gi*, and shows **Sources: …docs/architecture.md…** under the reply.

Then ask a follow-up:

> And the database volume?

It answers *1 Gi*. The follow-up doesn't say what it's about, so the server searched with the previous question too.

Now ask something unrelated:

> What is the capital of Japan? One word.

It answers *Tokyo*, with **no sources** line: nothing in the docs matched, so the model answered from its own knowledge.

**Say:** "The model was never trained on this project. Before answering, the server searches the documentation in Postgres, by meaning and by keywords. It adds the best-matching passages to the prompt, and the model answers from them and says where the answer came from. If nothing matches, it's an ordinary chat."

Show what the knowledge base holds:

```bash
k exec postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "SELECT source, count(*) AS chunks FROM chunks GROUP BY source ORDER BY source"'
```

Optional: show how adding a document works. Create a file in `knowledge/` with a made-up fact, for example a team's on-call rota. Then run `make k8s-up`, which rebuilds the image and re-ingests; only the new file is processed. Then ask the bot about it.

## 11. Logs (optional, 1 min)

```bash
make k8s-logs
```

Send a message in the browser and watch a `POST /api/chat` line appear. The prefix shows which pod handled it. Press Ctrl-C to stop.

---

## After the demo

Stop the cluster and free its memory. Its data is kept.

```bash
docker stop chatbot-control-plane
```

Start it again later:

```bash
docker start chatbot-control-plane
```

To delete everything, including the model download and all stored chats:

```bash
make k8s-down
```

## If something goes wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| Page doesn't load at :8090 | The cluster isn't running | `docker start chatbot-control-plane`, then `make k8s-status` |
| A reply takes 30+ seconds | The model is loading into memory after being idle | Wait. Later replies are faster |
| "can't reach Ollama" | The Ollama pod is restarting or out of memory | `k get pods`, then `k describe pod -l app.kubernetes.io/name=ollama` |
| "database unavailable" for more than a few seconds | Postgres isn't ready | `k get pods`, then `k logs postgres-0` |
| `ollama` pod shows `OOMKilled` | Docker is short on memory | Stop other kind clusters, or give Docker more memory |
