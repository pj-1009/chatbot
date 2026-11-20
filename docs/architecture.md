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

        ollama["Ollama<br/>Deployment, 1 replica<br/>model: gemma4:e2b"]
        pg[("PostgreSQL<br/>StatefulSet<br/>conversations, messages")]
        modelvol[("ollama-models<br/>15 Gi volume")]
        pgvol[("postgres data<br/>1 Gi volume")]
    end

    user -- "localhost port 8090" --> svc
    svc --> pod1
    svc --> pod2
    pod1 -- "load and save history" --> pg
    pod2 -- "load and save history" --> pg
    pod1 -- "POST /api/chat (streamed)" --> ollama
    pod2 -- "POST /api/chat (streamed)" --> ollama
    ollama --- modelvol
    pg --- pgvol
```

| Part | What it does |
|---|---|
| **Browser** (`static/index.html`) | Shows the chat. Keeps only a conversation ID (a UUID) in localStorage |
| **chatbot pods** (`server.py`, `db.py`) | Serve the page and the API. They hold no state, so any pod can answer any request |
| **PostgreSQL** | Stores every conversation's messages. Shared by all chatbot pods |
| **Ollama** | Runs the language model and streams its reply. Reachable only inside the cluster |
| **Volumes** | Keep the downloaded model and the stored messages across pod restarts |

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
    S->>D: Load the last 50 messages
    D-->>S: Earlier messages
    S->>O: System prompt + history + new message
    loop Until the reply is complete
        O-->>S: Next few words
        S-->>B: {"content": "..."}
        B-->>U: Words appear on screen
    end
    S->>D: Save the question and the reply together
    Note over S,D: Skipped if the reply failed,<br/>or the chat was reset meanwhile
    S-->>B: {"done": true}
```

The model doesn't remember anything between requests. "Memory" comes from step 4: the pod loads the earlier messages from PostgreSQL and sends them to the model with every new question.

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

## Running it without Kubernetes

`make serve` runs the same `server.py` on your Mac at http://localhost:8099. It uses the native Ollama app and a Postgres container on port 5433. `make run` starts the terminal chat (`chat.py`), which talks to Ollama directly and keeps its history in memory, without a database.
