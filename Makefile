.DEFAULT_GOAL := help

PLATFORM_ROOT := $(shell pwd)
COMPOSE := docker compose
KUBECTL := kubectl
OPA := opa

# ─────────────────────────────────────────────────────────────────
# Help
# ─────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo "Smart AI DevOps & Continuous Delivery Platform"
	@echo "================================================"
	@echo "  make setup            Full environment bootstrap (infra + Kind + migrations)"
	@echo "  make up               Start all services (docker compose up -d)"
	@echo "  make down             Stop all services"
	@echo "  make logs             Tail all service logs"
	@echo "  make migrate          Run alembic upgrade head"
	@echo "  make test-all         Run ALL test suites"
	@echo "  make test-stats       Run statistical robustness tests"
	@echo "  make test-guardrails  Run guardrail adversarial tests"
	@echo "  make test-opa         Run OPA policy tests"
	@echo "  make demo-healthy     Run healthy canary rollout demo"
	@echo "  make demo-fail        Run failure/rollback demo"
	@echo "  make kind-up          Create Kind cluster"
	@echo "  make kind-down        Delete Kind cluster"
	@echo "  make deploy-sample-app  Build+load sample-app images, apply gateway/canary manifests"
	@echo "  make connect-kind-network  Wire pipeline-worker/policy-controller to the real cluster"
	@echo "  make healthcheck      curl /healthz on all 5 services"
	@echo "  make clean            Remove all build artifacts"

# ─────────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────────
.PHONY: setup
setup: check-deps up-infra migrate kind-up install-envoy seed
	@echo "✅  Setup complete. Run 'make up' to start all services."

.PHONY: check-deps
check-deps:
	@command -v docker >/dev/null 2>&1 || (echo "❌ docker not found" && exit 1)
	@command -v kubectl >/dev/null 2>&1 || (echo "❌ kubectl not found" && exit 1)
	@command -v kind >/dev/null 2>&1 || (echo "❌ kind not found" && exit 1)
	@command -v opa >/dev/null 2>&1 || (echo "❌ opa not found" && exit 1)
	@command -v python3 >/dev/null 2>&1 || (echo "❌ python3 not found" && exit 1)
	@echo "✅  All dependencies found."

.PHONY: up-infra
up-infra:
	$(COMPOSE) up -d postgres redis opa minio
	@echo "⏳ Waiting for infrastructure to be healthy..."
	@sleep 5
	$(COMPOSE) ps

.PHONY: up
up:
	$(COMPOSE) up -d
	@echo "✅  All services started. Run 'make healthcheck' to verify."

.PHONY: down
down:
	$(COMPOSE) down

.PHONY: logs
logs:
	$(COMPOSE) logs -f --tail=100

# ─────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────
.PHONY: migrate
migrate:
	@echo "📦 Running Alembic migrations..."
	cd services/api-gateway && python -m alembic upgrade head
	@echo "✅  Migrations applied."

.PHONY: migrate-downgrade
migrate-downgrade:
	cd services/api-gateway && python -m alembic downgrade -1

.PHONY: migrate-history
migrate-history:
	cd services/api-gateway && python -m alembic history

.PHONY: seed
seed:
	@echo "🌱 Seeding test tenant..."
	$(COMPOSE) exec postgres psql -U platform -d platform -c \
		"INSERT INTO tenants (name) VALUES ('acme-corp') ON CONFLICT DO NOTHING;"

# ─────────────────────────────────────────────────────────────────
# Kind Cluster
# ─────────────────────────────────────────────────────────────────
.PHONY: kind-up
kind-up:
	@echo "☸️  Creating Kind cluster..."
	kind create cluster --config k8s/kind-config.yaml --name smartcd-local 2>/dev/null || \
		echo "Cluster already exists, skipping."
	kubectl cluster-info --context kind-smartcd-local

.PHONY: kind-down
kind-down:
	kind delete cluster --name smartcd-local

.PHONY: install-envoy
install-envoy:
	@echo "📦 Installing Envoy Gateway v1.9.1 + Gateway API CRDs..."
	kubectl apply --context kind-smartcd-local \
		-f https://github.com/envoyproxy/gateway/releases/download/v1.9.1/install.yaml
	kubectl create namespace production --context kind-smartcd-local 2>/dev/null || true
	@echo "⏳ Waiting for Envoy Gateway pods..."
	kubectl wait --namespace envoy-gateway-system \
		--for=condition=ready pod \
		--selector=app.kubernetes.io/name=gateway \
		--timeout=120s --context kind-smartcd-local || true

.PHONY: deploy-sample-app
deploy-sample-app:
	@echo "🐳 Building sample-app images..."
	docker build -t localhost:5001/payments:v1.0.0 services/sample-app/v1.0.0
	docker build -t localhost:5001/payments:v1.1.0 services/sample-app/v1.1.0
	@echo "📥 Loading images into Kind (no registry needed)..."
	kind load docker-image localhost:5001/payments:v1.0.0 --name smartcd-local
	kind load docker-image localhost:5001/payments:v1.1.0 --name smartcd-local
	@echo "☸️  Applying gateway + baseline/canary manifests..."
	kubectl apply --context kind-smartcd-local -f k8s/gateway/gateway-class.yaml
	kubectl apply --context kind-smartcd-local -f k8s/gateway/gateway.yaml
	kubectl apply --context kind-smartcd-local -f k8s/gateway/httproute-payments.yaml
	kubectl apply --context kind-smartcd-local -f k8s/payments-service/baseline-deployment.yaml
	kubectl apply --context kind-smartcd-local -f k8s/payments-service/canary-deployment.yaml
	@echo "✅  Deployed. Traffic starts at 100% baseline / 0% canary."

.PHONY: connect-kind-network
connect-kind-network:
	@bash scripts/setup/connect_kind_network.sh

# ─────────────────────────────────────────────────────────────────
# EKS Cluster (real AWS actuation target)
# ─────────────────────────────────────────────────────────────────
.PHONY: eks-up
eks-up:
	@echo "☁️  Creating EKS cluster (this bills your AWS account and takes ~15-20 min)..."
	eksctl create cluster -f k8s/eks-config.yaml

.PHONY: eks-down
eks-down:
	@echo "☁️  Deleting EKS cluster — run this between sessions to stop billing..."
	eksctl delete cluster -f k8s/eks-config.yaml

.PHONY: ecr-up
ecr-up:
	@echo "📦 Creating ECR repository (idempotent)..."
	@python -c "import boto3; c = boto3.client('ecr', region_name='us-east-1'); \
	c.create_repository(repositoryName='payments')" 2>/dev/null || echo "Repository already exists."

.PHONY: install-envoy-eks
install-envoy-eks:
	@echo "📦 Installing Envoy Gateway v1.9.1 + Gateway API CRDs on EKS..."
	kubectl apply --context $$(kubectl config current-context) \
		-f https://github.com/envoyproxy/gateway/releases/download/v1.9.1/install.yaml
	kubectl create namespace production 2>/dev/null || true
	@echo "⏳ Waiting for Envoy Gateway pods..."
	kubectl wait --namespace envoy-gateway-system \
		--for=condition=ready pod \
		--selector=app.kubernetes.io/name=gateway \
		--timeout=180s || true

.PHONY: deploy-sample-app-eks
deploy-sample-app-eks:
	@echo "🐳 Building and pushing sample-app images to ECR..."
	@python -c "import boto3; c = boto3.client('ecr', region_name='us-east-1'); \
	print(c.get_authorization_token()['authorizationData'][0]['authorizationToken'])" \
	| python -c "import sys, base64; print(base64.b64decode(sys.stdin.read()).decode().split(':', 1)[1])" \
	| docker login --username AWS --password-stdin 236087863083.dkr.ecr.us-east-1.amazonaws.com
	docker build -t 236087863083.dkr.ecr.us-east-1.amazonaws.com/payments:v1.0.0 services/sample-app/v1.0.0
	docker build -t 236087863083.dkr.ecr.us-east-1.amazonaws.com/payments:v1.1.0 services/sample-app/v1.1.0
	docker push 236087863083.dkr.ecr.us-east-1.amazonaws.com/payments:v1.0.0
	docker push 236087863083.dkr.ecr.us-east-1.amazonaws.com/payments:v1.1.0
	@echo "☸️  Applying gateway + baseline/canary manifests to EKS..."
	kubectl create namespace production 2>/dev/null || true
	kubectl apply -f k8s/gateway/gateway-class.yaml
	kubectl apply -f k8s/gateway/httproute-payments.yaml
	kubectl apply -f k8s/eks/payments-service/baseline-deployment.yaml
	kubectl apply -f k8s/eks/payments-service/canary-deployment.yaml
	@echo "✅  Deployed to EKS. Traffic starts at 100% baseline / 0% canary."

.PHONY: connect-eks
connect-eks:
	@bash scripts/setup/connect_eks.sh

.PHONY: verify-network-boundary
verify-network-boundary:
	@bash scripts/setup/verify_network_boundary.sh

.PHONY: scale-test-up
scale-test-up:
	docker compose -f docker-compose.yml -f docker-compose.scale-test.yml up -d --scale pipeline-worker=3 --scale policy-controller=3

.PHONY: scale-test-down
scale-test-down:
	docker compose -f docker-compose.yml -f docker-compose.scale-test.yml up -d --scale pipeline-worker=1 --scale policy-controller=1

# ─────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────
.PHONY: test-all
test-all: test-stats test-engine test-guardrails test-opa
	@echo "✅  All tests passed."

.PHONY: test-engine
test-engine:
	@echo "🧪 Running verification engine unit tests..."
	cd services/verification-engine && python -m pytest tests/ -v --tb=short

.PHONY: test-stats
test-stats:
	@echo "🧪 Running statistical robustness tests..."
	python -m pytest tests/adversarial/test_statistical_robustness.py -v --tb=short

.PHONY: test-guardrails
test-guardrails:
	@echo "🧪 Running guardrail adversarial tests..."
	python -m pytest tests/adversarial/test_guardrail_bypass.py -v --tb=short

.PHONY: test-opa
test-opa:
	@echo "🧪 Running OPA policy tests..."
	$(OPA) test policies/ -v

.PHONY: test-e2e
test-e2e:
	@echo "🧪 Running end-to-end tests..."
	python -m pytest tests/e2e/ -v --tb=short

# ─────────────────────────────────────────────────────────────────
# Demo Scenarios
# ─────────────────────────────────────────────────────────────────
.PHONY: demo-healthy
demo-healthy:
	@echo "🚀 Running healthy canary rollout demo..."
	bash scripts/demo/demo_healthy_rollout.sh

.PHONY: demo-fail
demo-fail:
	@echo "💥 Running failure/rollback demo..."
	bash scripts/demo/demo_failed_rollout.sh

# ─────────────────────────────────────────────────────────────────
# Health Checks
# ─────────────────────────────────────────────────────────────────
.PHONY: healthcheck
healthcheck:
	@echo "🏥 Checking all service health endpoints..."
	@curl -sf http://localhost:8000/healthz | python -m json.tool && echo "✅ api-gateway" || echo "❌ api-gateway"
	@curl -sf http://localhost:8002/healthz | python -m json.tool && echo "✅ verification-engine" || echo "❌ verification-engine"
	@curl -sf http://localhost:8003/healthz | python -m json.tool && echo "✅ policy-controller" || echo "❌ policy-controller"
	@curl -sf http://localhost:8004/healthz | python -m json.tool && echo "✅ explainability-service" || echo "❌ explainability-service"

# ─────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────
.PHONY: clean
clean:
	$(COMPOSE) down -v --remove-orphans
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "✅  Cleaned."
