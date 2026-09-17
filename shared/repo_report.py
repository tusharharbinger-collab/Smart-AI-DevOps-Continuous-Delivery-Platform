"""
shared/repo_report.py

Generic ML-based repository report and hosting cost prediction.
Evaluates repository build readiness, risk anomaly score (IsolationForest),
deterministic ECS Fargate hosting cost, and an AI/fallback summary narrative.

Design principle:
- Deterministic feature extraction and a real scikit-learn IsolationForest
  trained against a static reference corpus compute the objective metrics.
- Arithmetic using real AWS Fargate rates projects hosting costs.
- LLM (Groq) is strictly used to narrate findings; falls back gracefully
  to a deterministic template if unconfigured or unreachable.
"""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import httpx
import numpy as np
from sklearn.ensemble import IsolationForest
import structlog

from shared.fargate_pricing import FARGATE_CPU_COST_PER_VCPU_HOUR, FARGATE_MEM_COST_PER_GB_HOUR

logger = structlog.get_logger(__name__)

LOCKFILE_NAMES = frozenset({
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "go.sum",
    "Gemfile.lock",
    "composer.lock",
})

CI_CONFIG_PATTERNS = (
    ".github/workflows/",
    ".gitlab-ci.yml",
    ".circleci/",
    "azure-pipelines.yml",
    "Jenkinsfile",
)

README_NAMES = frozenset({"readme.md", "readme.txt", "readme", "readme.rst"})
LICENSE_NAMES = frozenset({"license", "license.md", "license.txt", "licence", "licence.md", "copying"})


@dataclass
class RepoFeatures:
    file_count: int
    max_depth: int
    has_tests: bool
    has_dockerfile: bool
    has_lockfile: bool
    has_ci_config: bool
    has_readme: bool
    has_license: bool
    dependency_count: int
    language: str | None = None
    framework: str | None = None
    build_confidence: str = "high"


def extract_repo_features(
    file_paths: list[str],
    package_json_content: dict | None = None,
    requirements_txt_content: str | None = None,
    detection=None,
) -> RepoFeatures:
    """
    Extracts structural repo features from file tree and manifests without cloning.
    """
    file_count = len(file_paths)
    max_depth = max((p.count("/") for p in file_paths), default=0)

    # Dockerfile detection
    has_dockerfile = any(
        p.rsplit("/", 1)[-1] in ("Dockerfile", "dockerfile") and p.count("/") <= 2
        for p in file_paths
    )

    # Tests detection (checks detection result, test directories, or test files)
    has_tests = False
    if detection and (getattr(detection, "test_config_found", False) or getattr(detection, "test_command", None)):
        has_tests = True
    else:
        for p in file_paths:
            low = p.lower()
            base = low.rsplit("/", 1)[-1]
            if (
                "test" in base
                or "spec" in base
                or "/tests/" in low
                or "/test/" in low
                or base.startswith("test_")
                or base.endswith("_test.go")
                or base.endswith(".test.js")
                or base.endswith(".test.ts")
                or base.endswith(".spec.ts")
            ):
                has_tests = True
                break

    # Lockfile detection
    has_lockfile = any(
        p.rsplit("/", 1)[-1] in LOCKFILE_NAMES and p.count("/") <= 2
        for p in file_paths
    )

    # CI workflow detection
    has_ci_config = any(
        any(p.startswith(pattern) or p == pattern for pattern in CI_CONFIG_PATTERNS)
        for p in file_paths
    )

    # README & License detection
    has_readme = any(p.rsplit("/", 1)[-1].lower() in README_NAMES and p.count("/") <= 1 for p in file_paths)
    has_license = any(p.rsplit("/", 1)[-1].lower() in LICENSE_NAMES and p.count("/") <= 1 for p in file_paths)

    # Dependency count
    dependency_count = 0
    if package_json_content and isinstance(package_json_content, dict):
        deps = package_json_content.get("dependencies", {})
        dev_deps = package_json_content.get("devDependencies", {})
        dependency_count = len(deps) + len(dev_deps)
    elif requirements_txt_content:
        lines = [
            line.strip()
            for line in requirements_txt_content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        dependency_count = len(lines)

    language = getattr(detection, "language", None) if detection else None
    framework = getattr(detection, "framework", None) if detection else None
    build_confidence = getattr(detection, "confidence", "low") if detection else ("high" if has_dockerfile else "low")

    return RepoFeatures(
        file_count=file_count,
        max_depth=max_depth,
        has_tests=has_tests,
        has_dockerfile=has_dockerfile,
        has_lockfile=has_lockfile,
        has_ci_config=has_ci_config,
        has_readme=has_readme,
        has_license=has_license,
        dependency_count=dependency_count,
        language=language,
        framework=framework,
        build_confidence=build_confidence,
    )


def _feature_vector(features: RepoFeatures) -> list[float]:
    return [
        float(features.file_count),
        float(features.max_depth),
        1.0 if features.has_tests else 0.0,
        1.0 if features.has_dockerfile else 0.0,
        1.0 if features.has_lockfile else 0.0,
        1.0 if features.has_ci_config else 0.0,
        1.0 if features.has_readme else 0.0,
        1.0 if features.has_license else 0.0,
        float(features.dependency_count),
    ]


def _load_reference_corpus(custom_path: Path | None = None) -> list[list[float]]:
    target = custom_path or (Path(__file__).resolve().parent / "data" / "repo_reference_profiles.json")
    if target.exists():
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
                profiles = data.get("profiles", [])
                if profiles:
                    return [
                        [
                            float(p.get("file_count", 25)),
                            float(p.get("max_depth", 4)),
                            float(p.get("has_tests", 1)),
                            float(p.get("has_dockerfile", 1)),
                            float(p.get("has_lockfile", 1)),
                            float(p.get("has_ci_config", 1)),
                            float(p.get("has_readme", 1)),
                            float(p.get("has_license", 1)),
                            float(p.get("dependency_count", 15)),
                        ]
                        for p in profiles
                    ]
        except Exception as e:
            logger.warning("failed_to_load_repo_reference_profiles", error=str(e))

    # Built-in synthetic fallback if file missing
    return [
        [30.0, 4.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 15.0],
        [45.0, 5.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 20.0],
        [20.0, 3.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, 10.0],
        [60.0, 5.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 25.0],
        [15.0, 2.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 5.0],
    ]


def score_repo_risk(features: RepoFeatures, reference_corpus_path: Path | None = None) -> dict:
    """
    Fits scikit-learn IsolationForest against the reference corpus plus incoming vector.
    Returns normalized risk_score (0-1), risk_level (low/medium/high), and explainable risk_flags.
    """
    ref_vectors = _load_reference_corpus(reference_corpus_path)
    cand_vec = _feature_vector(features)

    X_all = np.array(ref_vectors + [cand_vec], dtype=float)

    clf = IsolationForest(
        n_estimators=100,
        max_samples=min(256, len(X_all)),
        contamination=0.1,
        random_state=42,
    )
    clf.fit(X_all)

    # score_samples returns negative values (more negative = more anomalous)
    raw_scores = -clf.score_samples(X_all)
    span = raw_scores.max() - raw_scores.min()
    if span > 0:
        normalized_scores = (raw_scores - raw_scores.min()) / span
    else:
        normalized_scores = np.zeros_like(raw_scores)

    cand_score = float(normalized_scores[-1])

    if cand_score < 0.35:
        risk_level = "low"
    elif cand_score < 0.65:
        risk_level = "medium"
    else:
        risk_level = "high"

    # Compute explainable risk flags
    risk_flags: list[str] = []
    if not features.has_tests:
        risk_flags.append("No automated tests detected — pre-flight test suites will be skipped")
    if not features.has_lockfile:
        risk_flags.append("No lockfile found — dependency versions are unpinned, creating build reproducibility risk")
    if not features.has_ci_config:
        risk_flags.append("No CI workflow configuration found (.github/workflows or similar)")
    if not features.has_dockerfile:
        risk_flags.append("No Dockerfile found — will rely on automated container synthesis")
    if not features.has_readme:
        risk_flags.append("No README documentation file detected")
    if not features.has_license:
        risk_flags.append("No LICENSE file detected")
    if features.dependency_count == 0 and features.language in ("node", "python"):
        risk_flags.append("No application dependencies declared in manifest")
    if features.build_confidence == "low":
        risk_flags.append("Build readiness confidence is low — manual configuration required")

    return {
        "risk_score": round(cand_score, 4),
        "risk_level": risk_level,
        "risk_flags": risk_flags,
    }


def predict_hosting_cost(features: RepoFeatures, canary_step_hours: float = 2.0) -> dict:
    """
    Deterministic AWS Fargate hosting cost estimation based on standard ECS task sizing
    (cpu="256" = 0.25 vCPU, memory="512" = 0.5 GiB RAM).
    """
    vcpu = 0.25
    gib = 0.5
    hourly_per_task = (vcpu * FARGATE_CPU_COST_PER_VCPU_HOUR) + (gib * FARGATE_MEM_COST_PER_GB_HOUR)

    # 1 baseline replica running continuously for standard 730 hours/month
    steady_state_monthly_usd = round(hourly_per_task * 730.0, 2)

    # Extra canary replica active during canary evaluation window
    rollout_window_usd = round(hourly_per_task * canary_step_hours, 4)

    return {
        "task_cpu_units": 256,
        "task_memory_mib": 512,
        "task_cpu_vcpu": vcpu,
        "task_memory_gib": gib,
        "task_hourly_usd": round(hourly_per_task, 4),
        "steady_state_monthly_usd": steady_state_monthly_usd,
        "estimated_rollout_window_usd": rollout_window_usd,
        "canary_step_hours": canary_step_hours,
        "assumed_replicas": 1,
    }


async def build_narrative(
    risk_level: str,
    risk_flags: list[str],
    cost: dict,
    features: RepoFeatures,
    timeout_seconds: float = 15.0,
) -> str:
    """
    Generates a concise 1-2 sentence executive summary using Groq LLM if configured,
    or falls back deterministically.
    """
    monthly = cost.get("steady_state_monthly_usd", 8.42)
    canary = cost.get("estimated_rollout_window_usd", 0.02)

    # Deterministic fallback
    if not risk_flags:
        fallback = (
            f"Repository shows healthy structure with test and lockfile coverage. "
            f"Estimated hosting is ${monthly}/mo steady-state on AWS Fargate (+${canary} per canary rollout)."
        )
    else:
        flags_snippet = "; ".join(risk_flags[:2])
        fallback = (
            f"Repository identified with {len(risk_flags)} readiness observations ({flags_snippet}). "
            f"Estimated hosting is ${monthly}/mo on AWS Fargate (+${canary} per rollout)."
        )

    groq_api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not groq_api_key:
        return fallback

    groq_model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    prompt = (
        f"Repository Health Assessment:\n"
        f"- Risk level: {risk_level}\n"
        f"- Language: {features.language or 'Unknown'}, Framework: {features.framework or 'None'}\n"
        f"- Issues: {risk_flags if risk_flags else 'None'}\n"
        f"- Projected cost: ${monthly}/month on AWS Fargate\n\n"
        f"Write a professional 2-sentence executive summary for a DevOps engineer deciding whether to onboard this service."
    )

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_api_key}"},
                json={
                    "model": groq_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are an autonomous cloud delivery assistant. Be concise, factual, and strictly cite "
                                "the provided risk and cost figures."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 120,
                },
            )
            res.raise_for_status()
            data = res.json()
            narrative = data["choices"][0]["message"]["content"].strip()
            return narrative or fallback
    except Exception as e:
        logger.warning("groq_narrative_generation_fallback", error=str(e))
        return fallback
