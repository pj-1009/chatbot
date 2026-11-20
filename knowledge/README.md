# Knowledge base

Put Markdown (`.md`) or text (`.txt`) files here for the chatbot to answer from. Subfolders work too.

`make ingest` (on your machine) or `make k8s-ingest` (on the cluster) loads this folder, together with the project's own `README.md`, `prompt.md` and `docs/`. Only new or changed files are re-processed, and documents you delete from here are removed from the knowledge base.
