"""
scripts/generate_repo_reference_profiles.py

One-off utility script to generate shared/data/repo_reference_profiles.json.
Contains numeric feature vectors representing healthy, standard open-source
repositories across Python, Node.js, and Go.

Run manually to recreate or expand the reference corpus.
"""
import json
from pathlib import Path

REFERENCE_PROFILES = [
    # Well-structured Node.js repos (Express, Nest, Next, Fastify apps/libs)
    {"name": "express-sample-app", "file_count": 28, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 18},
    {"name": "nest-api-service", "file_count": 65, "max_depth": 5, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 32},
    {"name": "nextjs-web-frontend", "file_count": 48, "max_depth": 5, "has_tests": 1, "has_dockerfile": 0, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 24},
    {"name": "fastify-microservice", "file_count": 35, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 14},
    {"name": "react-vite-spa", "file_count": 32, "max_depth": 4, "has_tests": 1, "has_dockerfile": 0, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 21},
    {"name": "koa-backend", "file_count": 22, "max_depth": 3, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 12},
    {"name": "typescript-cli-tool", "file_count": 40, "max_depth": 4, "has_tests": 1, "has_dockerfile": 0, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 15},
    {"name": "graphql-apollo-server", "file_count": 52, "max_depth": 5, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 26},
    {"name": "node-socketio-chat", "file_count": 25, "max_depth": 3, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 0, "has_readme": 1, "has_license": 1, "dependency_count": 10},
    {"name": "electron-desktop-app", "file_count": 80, "max_depth": 6, "has_tests": 1, "has_dockerfile": 0, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 42},

    # Well-structured Python repos (FastAPI, Flask, Django, CLI, ML inference)
    {"name": "fastapi-realworld-example", "file_count": 42, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 16},
    {"name": "flask-restful-service", "file_count": 30, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 12},
    {"name": "django-starter-template", "file_count": 75, "max_depth": 5, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 28},
    {"name": "poetry-python-package", "file_count": 24, "max_depth": 3, "has_tests": 1, "has_dockerfile": 0, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 8},
    {"name": "pytorch-inference-service", "file_count": 38, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 22},
    {"name": "celery-worker-tasks", "file_count": 34, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 14},
    {"name": "scrapy-crawler-project", "file_count": 26, "max_depth": 3, "has_tests": 0, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 11},
    {"name": "click-cli-utility", "file_count": 19, "max_depth": 3, "has_tests": 1, "has_dockerfile": 0, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 6},
    {"name": "sqlalchemy-alembic-service", "file_count": 45, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 19},
    {"name": "tornado-websocket-gateway", "file_count": 33, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 13},

    # Well-structured Go repos (Gin, Echo, Fiber, standard library services)
    {"name": "gin-api-boilerplate", "file_count": 36, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 15},
    {"name": "echo-web-framework-app", "file_count": 41, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 18},
    {"name": "go-fiber-rest-starter", "file_count": 29, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 12},
    {"name": "cobra-cli-application", "file_count": 22, "max_depth": 3, "has_tests": 1, "has_dockerfile": 0, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 9},
    {"name": "grpc-microservice-go", "file_count": 46, "max_depth": 5, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 20},
    {"name": "go-redis-queue-worker", "file_count": 27, "max_depth": 3, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 8},
    {"name": "go-clean-architecture-template", "file_count": 58, "max_depth": 5, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 22},

    # Additional standard profiles (moderate/lean healthy repositories)
    {"name": "lean-docker-utility", "file_count": 12, "max_depth": 2, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 5},
    {"name": "static-landing-page", "file_count": 15, "max_depth": 3, "has_tests": 0, "has_dockerfile": 1, "has_lockfile": 0, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 0},
    {"name": "python-script-automation", "file_count": 14, "max_depth": 2, "has_tests": 1, "has_dockerfile": 0, "has_lockfile": 1, "has_ci_config": 0, "has_readme": 1, "has_license": 1, "dependency_count": 4},
    {"name": "monorepo-service-a", "file_count": 88, "max_depth": 6, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 35},
    {"name": "graphql-client-sdk", "file_count": 35, "max_depth": 4, "has_tests": 1, "has_dockerfile": 0, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 11},
    {"name": "node-etl-pipeline", "file_count": 39, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 17},
    {"name": "django-ecommerce-api", "file_count": 92, "max_depth": 6, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 38},
    {"name": "fastapi-auth-service", "file_count": 31, "max_depth": 4, "has_tests": 1, "has_dockerfile": 1, "has_lockfile": 1, "has_ci_config": 1, "has_readme": 1, "has_license": 1, "dependency_count": 14},
]


def generate():
    target_path = Path(__file__).resolve().parent.parent / "shared" / "data" / "repo_reference_profiles.json"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            "Static reference corpus of healthy, deployable open-source repositories "
            "across Python, Node.js, and Go. Used by shared/repo_report.py to fit an "
            "IsolationForest anomaly detector and calculate risk flags compared against "
            "median repo properties. Regenerate via scripts/generate_repo_reference_profiles.py."
        ),
        "profiles": REFERENCE_PROFILES,
    }
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Successfully generated {len(REFERENCE_PROFILES)} profiles in {target_path}")


if __name__ == "__main__":
    generate()
