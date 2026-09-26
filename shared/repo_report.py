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
    # True/False when build detection ran (is a test command configured?); None when unknown.
    has_test_command: bool | None = None


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
        has_test_command=(
            bool(getattr(detection, "test_config_found", False) or getattr(detection, "test_command", None))
            if detection else None
        ),
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


# Deployment-readiness checks. Each is (id, severity, weight, title, why it matters, how to fix). The weights say
# how much a missing check costs the 0-100 readiness score; "critical" means the platform cannot build it at all.
_CHECKS = (
    ("build", "critical", 25, "Build method is clear",
     "The platform cannot build what it cannot identify - without a Dockerfile or a recognizable language the "
     "pipeline has nothing to run.",
     "Add a Dockerfile at the repo root, or a smartcd.yaml declaring the language and start command."),
    ("tests", "important", 20, "Automated tests present",
     "Without tests nothing checks the code before it reaches users; the pre-flight test step is skipped and only "
     "live canary statistics stand between a bug and production.",
     "Add at least a smoke test and set the test command (or run your tests as a Dockerfile build step)."),
    ("lockfile", "important", 20, "Dependencies are pinned (lockfile)",
     "Unpinned dependencies can resolve to different versions on every build, so a rollout can ship code you never "
     "tested.",
     "Commit your lockfile (package-lock.json, poetry.lock, go.sum, ...) and install from it."),
    ("ci", "minor", 10, "CI workflow configured",
     "Nothing verifies pull requests before they merge, so broken commits can trigger a deploy pipeline.",
     "Add a .github/workflows file that builds and tests on every pull request."),
    ("readme", "minor", 10, "README present",
     "New teammates and on-call engineers have no starting point for how the service runs.",
     "Add a README covering how to run, configure and deploy the service."),
    ("license", "minor", 5, "License declared",
     "Without a license the code's reuse terms are undefined.",
     "Add a LICENSE file."),
    ("deps", "minor", 5, "Dependencies declared in the manifest",
     "An empty manifest usually means the manifest is incomplete, so the image may miss packages it needs at runtime.",
     "List runtime dependencies in package.json / requirements.txt."),
)


def _check_result(check_id: str, features: RepoFeatures) -> bool | None:
    """True = passed, False = failed, None = not applicable to this repo."""
    lang = features.language
    if check_id == "build":
        return features.has_dockerfile or features.build_confidence == "high"
    if check_id == "tests":
        if not features.has_tests:
            return False
        # Test files exist but nothing will run them before deploy: half credit, reported as its own finding.
        return "partial" if features.has_test_command is False else True
    if check_id == "lockfile":
        # A static site or a repo with no dependencies has nothing to pin.
        if lang in (None, "static") and features.dependency_count == 0:
            return None
        return features.has_lockfile
    if check_id == "ci":
        return features.has_ci_config
    if check_id == "readme":
        return features.has_readme
    if check_id == "license":
        return features.has_license
    if check_id == "deps":
        return None if lang not in ("node", "python") else features.dependency_count > 0
    return None


_SEVERITY_ORDER = {"critical": 0, "important": 1, "minor": 2}

# What to show when a check fails (the check titles above read as passed-states).
_PROBLEM = {
    "build": "No clear way to build this repo",
    "tests": "No automated tests",
    "lockfile": "Dependencies are not pinned",
    "ci": "No CI workflow",
    "readme": "No README",
    "license": "No LICENSE",
    "deps": "No dependencies declared",
}


def assess_readiness(features: RepoFeatures) -> dict:
    """
    Deterministic 0-100 readiness score over checks that actually matter for shipping. Unlike the anomaly score
    (how UNUSUAL the repo's structure is), this answers "what would go wrong deploying it, and what fixes that".
    """
    earned = total = 0
    findings: list[dict] = []
    passed: list[str] = []
    for check_id, severity, weight, title, why, fix in _CHECKS:
        result = _check_result(check_id, features)
        if result is None:
            continue
        total += weight
        if result is True:
            earned += weight
            passed.append(title)
            continue
        problem = _PROBLEM[check_id]
        if result == "partial":
            earned += weight / 2
            severity, problem = "minor", "Tests exist but no test command is set"
            why = "The test files will not run before deploy until a test command is configured."
            fix = "Set the pre-flight test command (for example npm test or pytest), or run the tests in a Dockerfile build step."
        elif check_id == "build" and features.language:
            # The language is known, so a Dockerfile can be generated - what is missing is how to start it.
            severity, problem = "important", "Start command needs your input"
            why = (f"A Dockerfile can be generated for {features.language}, but the platform cannot tell how to "
                   "start the app, so the build cannot be finished automatically.")
            fix = "Set the start command in the wizard (for Node, add scripts.start to package.json), or add a Dockerfile."
        findings.append({"id": check_id, "severity": severity, "weight": weight, "title": title, "problem": problem, "why": why, "fix": fix})
    findings.sort(key=lambda f: (_SEVERITY_ORDER[f["severity"]], -f["weight"]))
    score = round(100 * earned / total) if total else 100
    has_critical = any(f["severity"] == "critical" for f in findings)
    level = "high" if has_critical or score < 40 else "medium" if score < 80 else "low"
    return {"readiness_score": score, "risk_level": level, "findings": findings, "passed": passed}


def score_repo_risk(features: RepoFeatures, reference_corpus_path: Path | None = None) -> dict:
    """
    Fits scikit-learn IsolationForest against the reference corpus plus incoming vector.
    Risk level and score come from the deterministic readiness assessment (assess_readiness); the IsolationForest
    output is returned separately as `anomaly_score`. Also returns explainable risk_flags and structured findings.
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

    anomaly_score = float(normalized_scores[-1])
    readiness = assess_readiness(features)
    risk_level = readiness["risk_level"]
    cand_score = 1.0 - readiness["readiness_score"] / 100.0

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
        "readiness_score": readiness["readiness_score"],
        "findings": readiness["findings"],
        "passed": readiness["passed"],
        # How unusual the structure is versus reference repos - informational, it does not drive the level.
        "anomaly_score": round(anomaly_score, 4),
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
    findings: list[dict] | None = None,
    readiness_score: int | None = None,
) -> str:
    """
    A short, specific summary: what the repo is, how ready it is, and the single most valuable fix. Uses Groq if
    configured, otherwise a deterministic sentence built from the same findings. It never mentions cost - pricing
    belongs after the infrastructure is built. (`cost` is accepted for call compatibility and deliberately unused.)
    """
    findings = findings or []
    what = " ".join(x for x in (features.language, features.framework) if x) or "repository"
    score_part = f"readiness {readiness_score}/100" if readiness_score is not None else f"{risk_level} risk"

    if not risk_flags and not findings:
        fallback = f"This {what} repo looks ready to onboard ({score_part}): build, tests and dependency pinning are all in place."
    else:
        top = findings[0] if findings else None
        listed = ", ".join(f.get("problem", f["title"]).lower() for f in findings[:3]) if findings else "; ".join(risk_flags[:2])
        fallback = f"This {what} repo scores {score_part}; gaps: {listed}."
        if top:
            fallback += f" Start with: {top['fix']}"

    groq_api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not groq_api_key:
        return fallback

    groq_model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    finding_lines = "\n".join(f"  - [{f['severity']}] {f.get('problem', f['title'])}: {f['fix']}" for f in findings) or "  none"
    prompt = (
        "Repository readiness assessment:\n"
        f"- Readiness score: {readiness_score if readiness_score is not None else 'n/a'}/100 ({risk_level} risk)\n"
        f"- Language: {features.language or 'Unknown'}, Framework: {features.framework or 'None'}\n"
        f"- Has Dockerfile: {features.has_dockerfile}\n"
        f"- Findings (most important first):\n{finding_lines}\n\n"
        "Write exactly 2 sentences for a DevOps engineer deciding whether to onboard this service: first the overall "
        "readiness in plain words, then the single most valuable fix to make first. Do not mention cost or pricing."
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
                                "only the provided findings and never invent problems."
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
