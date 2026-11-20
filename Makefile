.PHONY: help run venv db db-stop serve k8s-up k8s-status k8s-logs k8s-db k8s-down

KIND         ?= kind
KIND_CLUSTER := chatbot
K8S_DIR      := k8s
KCTX         := kubectl --context kind-$(KIND_CLUSTER)
KUBECTL      := $(KCTX) -n chatbot
PY           := .venv/bin/python

help: ## List the targets.
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

run: ## Chat in the terminal against the Ollama on this machine (keeps history in memory only).
	python3 chat.py

# --- Local web app ---------------------------------------------------------

# .env holds the local database password and is git-ignored. It's created
# once with a random password; delete it only together with the database
# container (make db-stop), because Postgres keeps the password it started with.
.env:
	@umask 077; printf 'PGHOST=127.0.0.1\nPGPORT=5433\nPGUSER=chatbot\nPGDATABASE=chatbot\nPGPASSWORD=%s\n' \
		"$$(openssl rand -hex 16)" > $@

$(PY): requirements.txt
	python3 -m venv .venv
	$(PY) -m pip install -q -r requirements.txt
	@touch $@

venv: $(PY) ## Create .venv with the dependencies.

db: .env ## Start a local Postgres in Docker on 127.0.0.1:5433.
	@set -a; . ./.env; set +a; \
	docker start chatbot-pg 2>/dev/null || docker run -d --name chatbot-pg \
		-e POSTGRES_USER="$$PGUSER" -e POSTGRES_PASSWORD="$$PGPASSWORD" -e POSTGRES_DB="$$PGDATABASE" \
		-p 127.0.0.1:5433:5432 postgres:17-alpine

db-stop: ## Remove the local Postgres container and its data.
	docker rm -f chatbot-pg

serve: $(PY) db ## Run the web app on http://localhost:8099 against the local Ollama and Postgres.
	@set -a; . ./.env; set +a; PORT=8099 $(PY) server.py

# --- Kubernetes (local kind cluster) --------------------------------------

# Database credentials for the cluster, created once with a random password.
# Delete it only together with the cluster (make k8s-down): the Postgres
# volume keeps the password it was first started with.
$(K8S_DIR)/db-secret.env:
	@umask 077; printf 'PGUSER=chatbot\nPGDATABASE=chatbot\nPGPASSWORD=%s\n' "$$(openssl rand -hex 16)" > $@

k8s-up: $(K8S_DIR)/db-secret.env ## Create the kind cluster if needed, build and load the image, deploy, wait until ready.
	@command -v $(KIND) >/dev/null || { echo "kind is not installed: brew install kind"; exit 1; }
	@$(KIND) get clusters 2>/dev/null | grep -qx $(KIND_CLUSTER) || \
		$(KIND) create cluster --name $(KIND_CLUSTER) --config $(K8S_DIR)/kind-cluster.yaml
	docker build -t chatbot:local .
	$(KIND) load docker-image --name $(KIND_CLUSTER) chatbot:local
	$(KCTX) apply -k $(K8S_DIR)
	@# The image tag stays "local", so restart the pods to pick up a fresh build.
	$(KUBECTL) rollout restart deployment/chatbot
	$(KUBECTL) rollout status statefulset/postgres --timeout=180s
	@echo "Waiting for Ollama. The first start downloads the model (several GB) and can take a while."
	$(KUBECTL) rollout status deployment/ollama --timeout=30m
	$(KUBECTL) rollout status deployment/chatbot --timeout=180s
	@echo "Chatbot: http://localhost:8090"

k8s-status: ## Show the pods, services and volumes.
	$(KUBECTL) get pods,services,pvc -o wide

k8s-logs: ## Follow the logs of every pod.
	$(KUBECTL) logs -f --prefix --all-containers --max-log-requests 10 \
		-l 'app.kubernetes.io/name in (chatbot,ollama,postgres)'

k8s-db: ## Open psql on the cluster's database.
	$(KUBECTL) exec -it statefulset/postgres -- sh -c 'psql -U "$$POSTGRES_USER" "$$POSTGRES_DB"'

k8s-down: ## Delete the kind cluster, including the model and all stored messages.
	$(KIND) delete cluster --name $(KIND_CLUSTER)
	rm -f $(K8S_DIR)/db-secret.env
