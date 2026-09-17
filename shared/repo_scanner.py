"""
shared/repo_scanner.py

Deterministic (no AI, no network) repository build-readiness detection.

Design decision, grounded in how Harness's own codebase-analysis actually
works (confirmed via their public docs): detection of "can this repo be
built, and how" is pure file-signature matching — Dockerfile present? A
recognized language manifest? A recognized test-framework config? — never
an LLM guessing from raw file dumps. An LLM's job (see explainability-
service's repo_advisor, a separate module) is to EXPLAIN and SYNTHESIZE a
human-readable checklist from these hard facts, never to BE the detector.
That split is what makes the platform's core promise possible: "when a
repo comes in, we will produce a build unless the failure is genuinely on
the human's side" — a promise an LLM's non-determinism can't back, but a
file-existence check can.

Two build methods, funneling into the exact same `docker build` call in
pipeline-worker's build_task.py either way:
  - "dockerfile": the repo already has one — use it as-is.
  - "synthesized": no Dockerfile, but a recognized language manifest exists
    — pipeline-worker generates a minimal, standard Dockerfile for that
    language (see tasks/dockerfile_synthesis.py) and builds THAT.
A repo with neither is not a platform failure — it's a real, correctly-
reported human-side gap ("no Dockerfile and no requirements.txt/package.json/
go.mod found — add one of these, or a Dockerfile, at repo root or the
folder you configure as root_directory").
"""
from dataclasses import dataclass, field

# Ordered: first match wins when multiple manifests coexist (rare, but a
# repo with both requirements.txt AND package.json — e.g. a Python backend
# with a JS frontend subfolder — should build the backend by default; the
# human can always override via root_directory in the wizard).
LANGUAGE_MANIFESTS: list[tuple[str, str]] = [
    ("requirements.txt", "python"),
    ("Pipfile", "python"),
    ("pyproject.toml", "python"),
    ("package.json", "node"),
    ("go.mod", "go"),
    ("pom.xml", "java-maven"),
    ("build.gradle", "java-gradle"),
    ("Gemfile", "ruby"),
    ("composer.json", "php"),
]

# File-existence signals only — good enough to tell the human "tests were
# found" / "no test setup detected", never used to auto-write a test
# command (that stays a human-confirmed field regardless of confidence,
# since a wrong guess here silently reports false test coverage).
TEST_CONFIG_SIGNALS: list[str] = [
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "jest.config.js",
    "jest.config.ts",
    "jest.config.json",
    "vitest.config.ts",
    "vitest.config.js",
]

# Basename only — matches "index.html" AND "public/index.html"/"dist/index.html"
# etc. via _find_candidates' own basename matching, so no separate constant
# per candidate folder is needed.
STATIC_INDEX_FILENAME = "index.html"

DOCKERFILE_NAMES = {"Dockerfile", "dockerfile"}
# Checked BEFORE Dockerfile/language-manifest detection — an explicit human
# declaration always outranks an inference. Schema (see parse_yaml_manifest):
#   runtime: docker | python | node | go | java-maven | java-gradle | ruby | php
#   dockerfilePath: Dockerfile      # only read when runtime: docker
#   startCommand: "..."             # required unless runtime: docker
#   testCommand: "..."              # optional
#   deploy: {...}                   # reserved for the deployment phase — parsed, never acted on
MANIFEST_FILENAMES = {"smartcd.yaml", "smartcd.yml"}
SYNTHESIS_SUPPORTED_LANGUAGES = frozenset({"python", "node", "go", "static"})
# Search depth for an un-declared Dockerfile/manifest — shallow on purpose:
# a Dockerfile 4 folders deep in an unrelated vendored dependency is noise,
# not a real candidate. Matches how root_directory-based projects in this
# platform are already expected to be shallow (service-per-folder, not a
# deeply nested monorepo).
MAX_SEARCH_DEPTH = 2


@dataclass
class BuildDetection:
    method: str  # "yaml_manifest" | "dockerfile" | "synthesized" | "unsupported"
    dockerfile_path: str | None = None
    language: str | None = None
    manifest_path: str | None = None
    start_command: str | None = None  # only ever a real signal (e.g. package.json's "scripts.start"), never guessed
    test_command: str | None = None   # only ever set by an explicit yaml_manifest declaration
    # Guaranteed Live Web App CI/CD — only ever set when language == "node",
    # a real file-signature detection of package.json's own dependencies
    # (never guessed): "spa" (Vite/CRA), "nextjs", or "node-server" (plain
    # Node backend, today's original behavior). Picks which multi-stage
    # Dockerfile template dockerfile_synthesis.py uses for a Node project —
    # the single-stage `npm install --omit=dev` template that treats every
    # Node repo as a plain server broke any Vite/React/Next build that needs
    # devDependencies to even produce output. None for every other language.
    framework: str | None = None
    test_config_found: bool = False
    confidence: str = "low"  # "high" | "low" — drives the confirm-vs-flag UI split
    issues: list[str] = field(default_factory=list)
    # Reserved for the deployment phase (Phase 9.5+) — parsed from a
    # yaml_manifest's `deploy:` block and carried forward, never read by
    # anything that acts on it yet. Keeping it here now means the schema
    # doesn't change shape later just because deployment work starts.
    deploy_config: dict | None = None


class YamlManifestError(Exception):
    pass


def parse_yaml_manifest(yaml_text: str) -> BuildDetection:
    """
    An explicit `smartcd.yaml` always wins over inference — every field here
    is exactly what the human wrote, so this is always confidence="high"
    (a human declaration can still be WRONG, but it's never ambiguous the
    way a guessed signal is). `runtime: docker` defers to the repo's real
    Dockerfile at `dockerfilePath` instead of synthesizing one.
    """
    import yaml

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise YamlManifestError(f"smartcd.yaml is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise YamlManifestError("smartcd.yaml must be a YAML mapping (key: value pairs) at the top level.")

    runtime = data.get("runtime")
    if not runtime or not isinstance(runtime, str):
        raise YamlManifestError("smartcd.yaml is missing required field 'runtime' (e.g. \"python\", \"docker\").")

    if runtime == "docker":
        dockerfile_path = data.get("dockerfilePath") or "Dockerfile"
        return BuildDetection(
            method="yaml_manifest",
            dockerfile_path=dockerfile_path,
            test_command=data.get("testCommand"),
            confidence="high",
            deploy_config=data.get("deploy"),
        )

    start_command = data.get("startCommand")
    issues = []
    if not start_command and runtime != "static":
        # "static" is the one runtime with no process to start — a static
        # site is just files nginx serves, never a command.
        issues.append("smartcd.yaml declares runtime but no 'startCommand' — required for any non-docker runtime.")
    if runtime not in SYNTHESIS_SUPPORTED_LANGUAGES:
        issues.append(
            f"runtime '{runtime}' has no Dockerfile-synthesis template yet (supported: "
            f"{sorted(SYNTHESIS_SUPPORTED_LANGUAGES)}) — declare runtime: docker with a real "
            f"Dockerfile instead, or ask for this language's template to be added."
        )
    return BuildDetection(
        method="yaml_manifest",
        language=runtime,
        start_command=start_command,
        test_command=data.get("testCommand"),
        confidence="high" if not issues else "low",
        issues=issues,
        deploy_config=data.get("deploy"),
    )


def _depth(path: str) -> int:
    return path.count("/")


def _find_candidates(file_paths: list[str], names: set[str] | list[str]) -> list[str]:
    name_set = set(names) if not isinstance(names, set) else names
    return sorted(
        (p for p in file_paths if p.rsplit("/", 1)[-1] in name_set and _depth(p) <= MAX_SEARCH_DEPTH),
        key=_depth,
    )


def _detect_node_framework(package_json_content: dict | None) -> str:
    """
    File-signature only, same discipline as everything else in this module
    — a real dependency name in package.json, never a guess. Checked in
    `dependencies` AND `devDependencies` since Vite/Next are legitimately
    declared in either depending on how a project's own package.json is
    laid out. Order matters: a Next.js app also depends on `react`, and
    some also add `vite` for tooling unrelated to the actual build, so
    Next.js is checked first — its own dependency is the more specific,
    unambiguous signal.
    """
    if not package_json_content:
        return "node-server"
    deps = {
        **(package_json_content.get("dependencies") or {}),
        **(package_json_content.get("devDependencies") or {}),
    }
    if "next" in deps:
        return "nextjs"
    if "vite" in deps or "react-scripts" in deps:
        return "spa"
    return "node-server"


def _resolve_manifest_path(detection: BuildDetection, yaml_manifest_path: str | None, file_paths: list[str]) -> None:
    """
    Real gap found live: a smartcd.yaml declaring a non-docker runtime never
    records its own location — parse_yaml_manifest only ever sees the raw
    YAML text, not the path it was fetched from — so a manifest living in a
    subfolder (e.g. nodocker/smartcd.yaml) left `manifest_path` unset.
    dockerfile_synthesis.py's caller-side default then looked for the
    language's manifest at repo ROOT and every build failed with a false
    "file not found", even though the exact same language + startCommand
    builds fine once manifest_path points at the real location. Mutates
    `detection` in place: verifies a real language-manifest file sits next
    to the yaml manifest — the same file-signature matching used everywhere
    else in this module — rather than defaulting blind at build time.
    """
    if detection.method != "yaml_manifest" or not detection.language or detection.dockerfile_path or detection.manifest_path:
        return
    yaml_dir = yaml_manifest_path.rsplit("/", 1)[0] if yaml_manifest_path and "/" in yaml_manifest_path else ""
    candidate_names = [name for name, lang in LANGUAGE_MANIFESTS if lang == detection.language]
    found = next(
        (candidate for name in candidate_names if (candidate := "/".join(p for p in [yaml_dir, name] if p)) in file_paths),
        None,
    )
    if found:
        detection.manifest_path = found
    elif candidate_names:
        location = f"in {yaml_dir}/" if yaml_dir else "at the repo root"
        detection.issues.append(
            f"smartcd.yaml declares runtime '{detection.language}' but none of "
            f"{candidate_names} were found {location}, next to the manifest — add one, "
            f"or declare a Dockerfile instead."
        )
        detection.confidence = "low"


def detect_build_method(
    file_paths: list[str],
    package_json_content: dict | None = None,
    yaml_manifest_content: str | None = None,
    yaml_manifest_path: str | None = None,
) -> BuildDetection:
    """
    `file_paths`: every file path in the repo (or repo subtree), forward-
    slash separated, as returned by GitHub's git-trees API.
    `package_json_content`: parsed package.json, if one was found and
    fetched — used ONLY to read a real `scripts.start` value, never to
    guess one.
    `yaml_manifest_content`: raw text of a found `smartcd.yaml`/`.yml`, if
    any — checked FIRST, before Dockerfile/language-manifest inference. A
    malformed manifest is reported as its own issue rather than silently
    falling through to inference, so a typo doesn't quietly produce a
    different build than the human intended.
    `yaml_manifest_path`: the repo path the manifest was actually found at
    (e.g. "nodocker/smartcd.yaml") — used only to resolve a language
    runtime's real manifest location (see `_resolve_manifest_path`), never
    to change WHICH build method is chosen.
    """
    if yaml_manifest_content is not None:
        try:
            detection = parse_yaml_manifest(yaml_manifest_content)
        except YamlManifestError as e:
            return BuildDetection(method="unsupported", confidence="high", issues=[str(e)])
        _resolve_manifest_path(detection, yaml_manifest_path, file_paths)
        return detection

    dockerfile_candidates = _find_candidates(file_paths, DOCKERFILE_NAMES)
    test_config_found = bool(_find_candidates(file_paths, TEST_CONFIG_SIGNALS)) or any(
        "/tests/" in f"/{p}" or "/test/" in f"/{p}" for p in file_paths
    )

    if dockerfile_candidates:
        return BuildDetection(
            method="dockerfile",
            dockerfile_path=dockerfile_candidates[0],
            test_config_found=test_config_found,
            confidence="high",
        )

    for manifest_name, language in LANGUAGE_MANIFESTS:
        candidates = _find_candidates(file_paths, {manifest_name})
        if not candidates:
            continue
        manifest_path = candidates[0]
        start_command = None
        framework = _detect_node_framework(package_json_content) if language == "node" else None
        if language == "node" and package_json_content:
            scripts = package_json_content.get("scripts") or {}
            if isinstance(scripts.get("start"), str) and scripts["start"].strip():
                start_command = "npm start"
        # A Vite/React app is served by nginx after a build step — it has
        # no runtime "start command" at all, unlike a plain Node server or
        # Next.js (both really do exec a process). Requiring one from the
        # human here would be asking for something that doesn't exist.
        has_build_script = bool(
            package_json_content
            and isinstance((package_json_content.get("scripts") or {}).get("build"), str)
            and (package_json_content["scripts"]["build"] or "").strip()
        )
        issues = []
        if language == "node" and framework == "spa":
            if not has_build_script:
                issues.append(
                    f"No Dockerfile found; detected a Vite/React app via {manifest_path}, but no "
                    f"\"scripts.build\" in package.json — needed to produce the static output nginx serves."
                )
        elif language == "node" and start_command:
            pass  # a real signal was found — nothing for the human to fill in
        elif language == "node":
            issues.append(
                f"No Dockerfile found; detected Node.js via {manifest_path}, but no "
                f"\"scripts.start\" in package.json — a start command must be entered manually."
            )
        elif language == "python":
            issues.append(
                f"No Dockerfile found; detected Python via {manifest_path} — a start "
                f"command (e.g. \"uvicorn main:app --host 0.0.0.0\") must be entered manually."
            )
        else:
            issues.append(
                f"No Dockerfile found; detected {language} via {manifest_path} — a start "
                f"command must be entered manually, and this language's synthesized-Dockerfile "
                f"template should be double-checked (less commonly exercised than Python/Node)."
            )
        confident = bool(start_command) or (framework == "spa" and has_build_script)
        return BuildDetection(
            method="synthesized",
            language=language,
            framework=framework,
            manifest_path=manifest_path,
            start_command=start_command,
            test_config_found=test_config_found,
            confidence="high" if confident else "low",
            issues=issues,
        )

    # Real gap this closes: a static site (no Dockerfile, no package.json/
    # requirements.txt/etc.) had no detection path at all and fell straight
    # through to "unsupported" — even though a real index.html is as
    # concrete and unambiguous a build signal as a Dockerfile itself.
    static_candidates = _find_candidates(file_paths, {STATIC_INDEX_FILENAME})
    if static_candidates:
        return BuildDetection(
            method="synthesized",
            language="static",
            manifest_path=static_candidates[0],
            test_config_found=test_config_found,
            confidence="high",
        )

    return BuildDetection(
        method="unsupported",
        test_config_found=test_config_found,
        confidence="high",
        issues=[
            "No Dockerfile and no recognized language manifest "
            "(requirements.txt, package.json, go.mod, pom.xml, build.gradle, Gemfile, composer.json) "
            "found within 2 folders of the repo root. Add one of these, or point root_directory "
            "at the subfolder that has one."
        ],
    )


# Guaranteed Live Web App CI/CD — real gap this closes: the wizard's
# Networking defaults were a flat `/healthz` + port 8080 regardless of what
# was actually detected. A real ECS target group health check hits this
# path directly (bypassing the ALB's own path-prefix routing entirely —
# see CLAUDE.md's ALB-path-prefix trap), and an arbitrary web app almost
# never implements a literal `/healthz` route, so ECS killed the task
# repeatedly for "failing health checks" even when the app itself was
# completely healthy. A web-facing app (static HTML, a Vite/CRA SPA, or
# Next.js) is suggested "/" instead, since a catch-all/SPA-fallback
# response at root is what these actually serve; an API-style service
# (plain Node server, Python, Go, a real Dockerfile, or an explicit
# smartcd.yaml) keeps the existing "/healthz" suggestion, since that IS a
# real, common convention for that category and guessing "/" for an API
# expecting JSON requests would be worse. Deliberately just a SUGGESTION
# returned to the wizard, never applied silently server-side — the human
# still confirms it, matching this module's "never guess" boundary for
# start_command.
_WEB_FACING_FRAMEWORKS = frozenset({"spa", "nextjs"})


def suggest_networking_defaults(detection: BuildDetection) -> dict:
    if detection.language == "static" or detection.framework in _WEB_FACING_FRAMEWORKS:
        port = 3000 if detection.framework == "nextjs" else 80
        return {"suggested_health_check_path": "/", "suggested_port": port}
    return {"suggested_health_check_path": "/healthz", "suggested_port": 8080}
