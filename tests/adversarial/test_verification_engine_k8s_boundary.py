"""
tests/adversarial/test_verification_engine_k8s_boundary.py

Phase 4 (§04-security-hardening.md, deliverable 4.5): "verification-engine
cannot touch Kubernetes" is invariant #3 (CLAUDE.md) — enforced today purely
at the code level (no `kubernetes` package, no kubeconfig mount). A real
Kubernetes `NetworkPolicy` would be the natural network-level reinforcement,
but doesn't apply here: verification-engine only ever runs as a
docker-compose service, never as a pod in the Kind cluster, so there is no
cluster-scoped object to attach a NetworkPolicy to.

Investigating this live (see docs/roadmap/04-security-hardening.md's "honest
scope note") found something worth guarding against permanently: Docker
Desktop's bridge-network isolation is weaker than native Linux dockerd's —
verification-engine (only on the compose default network, never `kind`)
could still open a raw TCP connection to the Kind control-plane's API
server address, even though it isn't declared on that network. Raw
reachability turned out not to matter because there is no `kubernetes`
client and no credentials — an actual API call authenticates as
`system:anonymous` and is rejected 403 by Kubernetes' own RBAC — but that
means the CODE-level boundary (no library, no kubeconfig) is currently
carrying the entire weight of this invariant, not the network. These tests
make sure nobody removes it by accident.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VERIFICATION_ENGINE_DIR = REPO_ROOT / "services" / "verification-engine"


def _iter_python_files():
    return (VERIFICATION_ENGINE_DIR / "src").rglob("*.py")


def test_requirements_txt_never_declares_a_kubernetes_client():
    requirements = (VERIFICATION_ENGINE_DIR / "requirements.txt").read_text(encoding="utf-8").lower()
    for forbidden in ("kubernetes", "kubernetes-client", "kubernetes_asyncio"):
        assert forbidden not in requirements, (
            f"verification-engine/requirements.txt declares '{forbidden}' — this service must never "
            "gain the ability to construct a Kubernetes API client (invariant #3, CLAUDE.md)."
        )


def test_source_never_imports_a_kubernetes_client_library():
    """
    Parses (doesn't just grep) every .py file's AST for an `import kubernetes`
    or `from kubernetes import ...` — catches the import however it's
    written (aliased, multi-name, nested) without false-positiving on an
    unrelated identifier that merely contains the word "kubernetes".
    """
    offending_files = []
    for path in _iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "kubernetes" or alias.name.startswith("kubernetes.") for alias in node.names
            ):
                offending_files.append(str(path))
            elif isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "kubernetes" or node.module.startswith("kubernetes.")
            ):
                offending_files.append(str(path))

    assert not offending_files, (
        f"Found a `kubernetes` client import in verification-engine source: {offending_files} — "
        "this service is structurally barred from touching Kubernetes (invariant #3, CLAUDE.md)."
    )


def test_no_kubeconfig_or_k8s_api_env_vars_referenced():
    """
    A service could still gain de-facto Kubernetes access without the
    `kubernetes` package by hand-rolling raw HTTP calls against a mounted
    kubeconfig or an in-cluster service-account token — guard against the
    two ways that credential would plausibly be discovered: an explicit
    KUBECONFIG-style env var lookup, or reading the in-cluster service
    account token path Kubernetes injects into every real pod.
    """
    forbidden_snippets = ("KUBECONFIG", "/var/run/secrets/kubernetes.io", "load_incluster_config", "load_kube_config")
    offending = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in text:
                offending.append((str(path), snippet))

    assert not offending, (
        f"verification-engine source references Kubernetes credential-discovery mechanisms: {offending} — "
        "this service must have no path to authenticating against a Kubernetes API server."
    )
