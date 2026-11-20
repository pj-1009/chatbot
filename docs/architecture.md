# Architecture

How the chatbot is put together, and what happens when you send a message.

## Components

```mermaid
flowchart TB
    user(["You, in a browser"])

    subgraph cluster["kind cluster: chatbot"]
        direction TB
        svc["chatbot Service<br/>NodePort 30090"]

        subgraph app["chatbot Deployment"]
            direction LR
            pod1["chatbot pod<br/>server.py"]
            pod2["chatbot pod<br/>server.py"]
        end

        ingest["ingest Job<br/>ingest.py<br/>run by make k8s-ingest"]
        ollama["Ollama<br/>Deployment, 1 replica<br/>chat: gemma4:e2b<br/>embeddings: nomic-embed-text"]
        pg[("PostgreSQL + pgvector<br/>StatefulSet<br/>conversations, messages,<br/>documents, chunks")]
        modelvol[("ollama-models<br/>15 Gi volume")]
        pgvol[("postgres data<br/>1 Gi volume")]
    end

    user -- "localhost port 8090" --> svc
    svc --> pod1
    svc --> pod2
    pod1 -- "history and knowledge search" --> pg
    pod2 -- "history and knowledge search" --> pg
    pod1 -- "embed question, then chat (streamed)" --> ollama
    pod2 -- "embed question, then chat (streamed)" --> ollama
    ingest -- "embed document chunks" --> ollama
    ingest -- "store chunks" --> pg
    ollama --- modelvol
    pg --- pgvol
```

| Part | What it does |
|---|---|
| **Browser** (`static/index.html`) | Shows the chat and the sources each answer used. Keeps only a conversation ID (a UUID) in localStorage |
| **chatbot pods** (`server.py`, `db.py`, `rag.py`) | Serve the page and the API, and search the knowledge base for each question. They hold no state, so any pod can answer any request |
| **ingest Job** (`ingest.py`) | Splits the project's docs into chunks, embeds them and stores them. Runs on demand, not continuously |
| **PostgreSQL + pgvector** | Stores every conversation's messages, plus the knowledge base: document chunks with their embeddings. Shared by all pods |
| **Ollama** | Runs two models: `gemma4:e2b` writes the replies, and `nomic-embed-text` turns text into embeddings for search. Reachable only inside the cluster |
| **Volumes** | Keep the downloaded models and the database across pod restarts |

## Sending a message

```mermaid
sequenceDiagram
    autonumber
    actor U as You
    participant B as Browser
    participant S as chatbot pod
    participant D as PostgreSQL
    participant O as Ollama

    U->>B: Type a message, press Enter
    B->>S: POST /api/chat {conversation_id, message}
    S->>D: Create the conversation if it's new
    S->>D: Load the last 20 messages
    D-->>S: Earlier messages
    S->>O: Embed the question (nomic-embed-text)
    O-->>S: Question embedding
    S->>D: Search the knowledge base: by meaning and by keywords
    D-->>S: Up to 4 relevant chunks, or none
    S-->>B: {"sources": [...]}
    S->>O: System prompt + chunks + history + new message
    loop Until the reply is complete
        O-->>S: Next few words
        S-->>B: {"content": "..."}
        B-->>U: Words appear on screen
    end
    S->>D: Save the question and the reply together
    Note over S,D: Skipped if the reply failed,<br/>or the chat was reset meanwhile
    S-->>B: {"done": true}
    B-->>U: Sources shown under the reply
```

The model doesn't remember anything between requests, and it knows nothing about this project on its own. Two lookups give it both:

- **Memory (steps 4–5):** the pod loads the conversation's earlier messages from PostgreSQL and sends them along with every new question.
- **Knowledge (steps 6–9), which is RAG:** the pod looks up the passages of the project's documentation that match the question, and adds them to the prompt. The model answers from them and names the document it used.

## How the knowledge search works

```mermaid
flowchart LR
    q["Question<br/>(plus the previous one,<br/>if it's a follow-up)"]
    q --> e["Embedding<br/>768 numbers"]
    q --> k["Keywords"]
    e --> v["By meaning:<br/>closest chunks,<br/>cosine distance ≤ 0.38"]
    k --> f["By keywords:<br/>full-text match covering<br/>over half the words"]
    v --> m["Merge the two rankings<br/>(reciprocal rank fusion)"]
    f --> m
    m --> top["Top 4 chunks<br/>(none if nothing passes)"]
```

- **Search by meaning** finds passages that say the same thing in other words.
- **Search by keywords** finds exact names that embeddings handle poorly, like `ollama-models` or `k8s-db`.
- **Both have a cut-off**, so a question unrelated to the docs ("write a poem") gets no excerpts, and the model answers normally.

## Loading the knowledge base

```mermaid
flowchart LR
    files["README.md, prompt.md,<br/>docs/, knowledge/"] --> changed{"Changed since<br/>last run?"}
    changed -- "No" --> skip["Skip"]
    changed -- "Yes" --> split["Split into chunks<br/>by heading, about 700 characters"]
    split --> embed["Embed each chunk<br/>(Ollama, nomic-embed-text)"]
    embed --> store["Replace the document's chunks<br/>in PostgreSQL"]
```

On the cluster, these files are baked into the chatbot image. `make k8s-up` rebuilds the image and then runs the ingest job, so a changed document is picked up on the next deploy. Documents removed from those folders are removed from the knowledge base.

## Other flows

```mermaid
flowchart TD
    subgraph reload["Reloading the page"]
        r1["Page loads"] --> r2{"Conversation ID<br/>in localStorage?"}
        r2 -- "No" --> r3["Create a new ID<br/>and show an empty chat"]
        r2 -- "Yes" --> r4["GET /api/history"] --> r5["Show the earlier messages"]
    end

    subgraph reset["Clicking New chat"]
        n1["Click New chat"] --> n2["Stop any reply in progress"]
        n2 --> n3["Create a new ID<br/>and clear the screen"]
        n3 --> n4["POST /api/reset with the old ID"]
        n4 --> n5["PostgreSQL deletes the conversation<br/>and all its messages"]
    end
```

To see all of this in action, follow the [demo walkthrough](demo.md).

## Running it without Kubernetes

`make serve` runs the same `server.py` on your Mac at http://localhost:8099. It uses the native Ollama app and a pgvector Postgres container on port 5433. `make ingest` loads the knowledge base into that container; run `ollama pull nomic-embed-text` once first. `make run` starts the terminal chat (`chat.py`), which talks to Ollama directly and keeps its history in memory, without a database or RAG.
