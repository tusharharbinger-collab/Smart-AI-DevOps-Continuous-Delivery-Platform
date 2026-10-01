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

# Infra-need signals — same discipline as everything else in this module:
# a real dependency name in a real manifest, never a guess. These drive the
# AI infra-generation flow's Requirements Form pre-fill (see
# AI_AGENTIC_ORCHESTRATION_PLAN.md §2.2) — the point is to hand that flow a
# real signal instead of it starting from a blank form, not to be
# exhaustive. `redis`/`ioredis` deliberately excluded from
# NODE_DB_PACKAGES/PYTHON_DB_PACKAGES (they're cache clients, not database
# clients) even though Redis is technically a database — this module treats
# it as the cache category since that's how every golden-path archetype in
# the orchestration plan uses it.
NODE_DB_PACKAGES: frozenset[str] = frozenset(
    {"pg", "mysql2", "mysql", "mongoose", "mongodb", "sequelize", "typeorm", "prisma", "knex"}
)
NODE_CACHE_PACKAGES: frozenset[str] = frozenset({"redis", "ioredis", "memcached"})
NODE_STORAGE_PACKAGES: frozenset[str] = frozenset(
    {"@aws-sdk/client-s3", "aws-sdk", "minio", "@google-cloud/storage"}
)

PYTHON_DB_PACKAGES: frozenset[str] = frozenset(
    {"psycopg2", "psycopg2-binary", "asyncpg", "pymongo", "sqlalchemy", "django", "mysqlclient", "pymysql"}
)
PYTHON_CACHE_PACKAGES: frozenset[str] = frozenset({"redis", "aioredis", "python-memcached", "pymemcache"})
PYTHON_STORAGE_PACKAGES: frozenset[str] = frozenset({"boto3", "minio", "google-cloud-storage"})


@dataclass
class InfraSignals:
    """
    Pure file-signature inference of what a repo's DEPLOYMENT likely needs —
    database/cache/object storage, and whether it produces static output
    only (no running process) — as opposed to BuildDetection's "how do we
    build it" question. Same "never guess beyond a real signal" rule: a
    False here means no matching dependency was found, not "confirmed not
    needed" — the Requirements Form (AI_AGENTIC_ORCHESTRATION_PLAN.md §2.5)
    always shows these as pre-filled-but-editable, never locked.
    """
    needs_database: bool = False
    database_hint: str | None = None  # the actual dependency name matched, for transparency in the UI
    needs_cache: bool = False
    cache_hint: str | None = None
    needs_object_storage: bool = False
    storage_hint: str | None = None
    # True when detect_build_method resolved to "static" (bare index.html)
    # or framework "spa" (Vite/CRA) — both produce build output served by
    # nginx with no running application process, the deciding signal for
    # the "static site" golden-path archetype vs. "stateless web service".
    is_static_site: bool = False
    # AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md Phase D — real, cited evidence from actually reading
    # source file content (never an LLM guess — see scan_source_evidence below), one CodeEvidence per
    # category that code-level scanning ADDED beyond what the manifest dependency check above already
    # found. Empty when no source scan ran, or every category was already covered by a manifest match.
    code_evidence: list["CodeEvidence"] = field(default_factory=list)


def _parse_requirements_txt_names(requirements_txt_content: str) -> set[str]:
    """
    requirements.txt has no structured parser in the stdlib worth pulling in
    for this — one package name per line, optionally followed by a version
    specifier (==, >=, <=, ~=, !=, <, >), an extras marker ([extra]), or an
    environment marker (; python_version...). Comments (#) and blank lines
    are skipped. Deliberately permissive (a line this doesn't parse cleanly
    is just dropped, never raises) since this is an inference aid, not a
    dependency resolver.
    """
    import re

    names: set[str] = set()
    for raw_line in requirements_txt_content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)", line)
        if match:
            names.add(match.group(1).lower())
    return names


def detect_infra_signals(
    package_json_content: dict | None,
    requirements_txt_content: str | None,
    detection: "BuildDetection",
) -> InfraSignals:
    """
    Reuses the exact package.json/requirements.txt content already fetched
    for detect_build_method — no new network calls, no re-parsing beyond
    what dependency names are present. `detection` is only consulted for
    `language`/`framework` (to decide `is_static_site`), never for infra
    inference — a Python or Node app's infra needs come from its own
    dependency manifest, not from which language it's written in.
    """
    signals = InfraSignals(
        is_static_site=(detection.language == "static" or detection.framework == "spa"),
    )

    if package_json_content:
        deps = {
            **(package_json_content.get("dependencies") or {}),
            **(package_json_content.get("devDependencies") or {}),
        }
        dep_names = {name.lower() for name in deps}
        if hit := next((n for n in NODE_DB_PACKAGES if n in dep_names), None):
            signals.needs_database, signals.database_hint = True, hit
        if hit := next((n for n in NODE_CACHE_PACKAGES if n in dep_names), None):
            signals.needs_cache, signals.cache_hint = True, hit
        if hit := next((n for n in NODE_STORAGE_PACKAGES if n in dep_names), None):
            signals.needs_object_storage, signals.storage_hint = True, hit

    if requirements_txt_content:
        req_names = _parse_requirements_txt_names(requirements_txt_content)
        if hit := next((n for n in PYTHON_DB_PACKAGES if n in req_names), None):
            signals.needs_database, signals.database_hint = True, hit
        if hit := next((n for n in PYTHON_CACHE_PACKAGES if n in req_names), None):
            signals.needs_cache, signals.cache_hint = True, hit
        if hit := next((n for n in PYTHON_STORAGE_PACKAGES if n in req_names), None):
            signals.needs_object_storage, signals.storage_hint = True, hit

    return signals


# ─────────────── Deep code-evidence scan (AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.1, Phase D) ───────────────
#
# `detect_infra_signals` above only ever looked at manifest DEPENDENCY NAMES (package.json/requirements.txt)
# — a real gap: "boto3 is in requirements.txt" doesn't distinguish a repo that actually calls S3 from one
# that imported it once and never used it. This scans actual source file CONTENT for real SDK call-site
# patterns instead — still pure regex/heuristic matching, no AI, no guessing (the same "never guess beyond
# a real signal" rule this module's docstring states as a permanent design choice) — just a richer signal
# than a bare dependency name, with a citation (file:line + the matched line itself) a human can verify.
#
# Deliberately bounded: this module does no network I/O itself (same as every other function here) — a
# caller (github_router.py's detect_build_config) selects which files to fetch via
# select_files_for_code_scan, fetches their content itself, and hands the resulting {path: content} dict
# to scan_source_evidence.

import re

MAX_CODE_SCAN_FILES = 15
MAX_CODE_SCAN_FILE_BYTES = 20_000

# Extensions worth reading for evidence, per detected language — deliberately narrow (source files only,
# never lockfiles/binaries/minified bundles) so a bounded scan stays meaningful.
_SCANNABLE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "node": (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"),
}

# Directories never worth scanning for evidence of what a repo's OWN code does — vendored/generated/test
# fixture content would produce false-positive "evidence" the human never actually wrote.
_SKIP_PATH_SUBSTRINGS = ("node_modules/", "vendor/", "dist/", "build/", ".venv/", "site-packages/", "/test/", "/tests/", "__pycache__/")


@dataclass(frozen=True)
class CodeEvidence:
    """One real, cited signal from actually reading a source file — never a bare boolean. `category`
    matches InfraSignals' fields (database/cache/object_storage/background_worker) so it slots directly
    into the same infra-need reasoning the manifest-based signals already feed."""
    file_path: str
    line_number: int
    snippet: str
    category: str


# (category, compiled pattern) — checked against actual source lines. Ordered by category the same way
# InfraSignals groups them. Deliberately call-SITE patterns (an actual client/connection construction),
# never a bare import line, since importing a package proves less than a dependency name already does.
_CODE_EVIDENCE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("object_storage", re.compile(r"boto3\.(client|resource)\(\s*[\"']s3[\"']")),
    ("object_storage", re.compile(r"\bMinioClient\(|new\s+Minio\(")),
    ("object_storage", re.compile(r"new\s+S3Client\(|new\s+AWS\.S3\(")),
    ("database", re.compile(r"psycopg2\.connect\(|create_engine\(|MongoClient\(|mysql\.connector\.connect\(")),
    ("database", re.compile(r"new\s+(Pool|Client)\(.*pg|mongoose\.connect\(|createConnection\(")),
    ("cache", re.compile(r"redis\.Redis\(|aioredis\.from_url\(|StrictRedis\(")),
    ("cache", re.compile(r"new\s+Redis\(|createClient\(\s*\{[^}]*redis")),
    ("background_worker", re.compile(r"celery\.Celery\(|@shared_task|@celery_app\.task")),
    ("background_worker", re.compile(r"\bcron\.schedule\(|node-cron|BullQueue\(|new\s+Queue\(")),
)


def infer_scan_language(file_paths: list[str]) -> str | None:
    """
    Real bug found live testing Phase D end-to-end: `BuildDetection.language` is ONLY ever set on the
    "synthesized" (no Dockerfile, language-manifest inferred) path — `_detect_build_method_core` returns
    immediately with `language=None` the instant a Dockerfile exists, since a Dockerfile means language
    inference is irrelevant to HOW the repo gets built. But a Dockerfile is the MOST common real case (it's
    checked first, before any language-manifest fallback), which meant the code-evidence scan below almost
    never ran in practice — silently correct (bounded/no-op is always safe), but silently useless for the
    overwhelming majority of real repos. This re-derives "what language is this repo's OWN source code
    written in" independently of build method, from the exact same manifest-presence signal
    `_detect_build_method_core` already uses (first match in `LANGUAGE_MANIFESTS` wins) — deciding what to
    scan for evidence is a genuinely different question from deciding how to build.
    """
    for manifest_name, language in LANGUAGE_MANIFESTS:
        if any(p.rsplit("/", 1)[-1] == manifest_name and p.count("/") <= 2 for p in file_paths):
            return language
    return None


def select_files_for_code_scan(file_paths: list[str], language: str | None) -> list[str]:
    """
    Deterministic, bounded selection: only real source files matching the detected language's extensions,
    outside vendored/generated/test directories, capped at MAX_CODE_SCAN_FILES so a caller's fetch cost
    stays small and predictable regardless of repo size. Preserves the tree's own ordering (no sorting by
    size/recency — GitHub's tree API gives no reliable size signal for this purpose) rather than sampling
    randomly, so the same repo always selects the same files.
    """
    extensions = _SCANNABLE_EXTENSIONS.get(language or "", ())
    if not extensions:
        return []
    selected = [
        p for p in file_paths
        if p.endswith(extensions) and not any(skip in f"/{p}" for skip in _SKIP_PATH_SUBSTRINGS)
    ]
    return selected[:MAX_CODE_SCAN_FILES]


def scan_source_evidence(file_contents: dict[str, str]) -> list[CodeEvidence]:
    """
    Pure, deterministic regex scan over already-fetched {path: content} — no I/O, no AI. Each file is
    capped at MAX_CODE_SCAN_FILE_BYTES (a huge generated/minified file truncated rather than skipped
    entirely, so evidence near the top of a large file is still found). Returns every match, not just the
    first per category — a caller decides how to fold multiple hits into one signal.
    """
    evidence: list[CodeEvidence] = []
    for path, content in file_contents.items():
        truncated = content[:MAX_CODE_SCAN_FILE_BYTES]
        for lineno, line in enumerate(truncated.splitlines(), start=1):
            for category, pattern in _CODE_EVIDENCE_PATTERNS:
                if pattern.search(line):
                    evidence.append(CodeEvidence(file_path=path, line_number=lineno, snippet=line.strip()[:200], category=category))
    return evidence


def apply_code_evidence(signals: InfraSignals, evidence: list[CodeEvidence]) -> InfraSignals:
    """
    Folds real code-evidence into an already-manifest-derived InfraSignals — additive only, never
    overrides or removes a manifest-based hint. A category the manifest already flagged keeps its
    original hint (the dependency name) as the primary signal but gains the code evidence as
    corroboration; a category the manifest MISSED but code evidence found gets turned on for the first
    time, with the evidence itself as the hint (there's no dependency name to cite instead).
    """
    signals.code_evidence = list(evidence)
    by_category: dict[str, CodeEvidence] = {}
    for e in evidence:
        by_category.setdefault(e.category, e)

    if "object_storage" in by_category and not signals.needs_object_storage:
        e = by_category["object_storage"]
        signals.needs_object_storage = True
        signals.storage_hint = f"{e.file_path}:{e.line_number} — {e.snippet}"
    if "database" in by_category and not signals.needs_database:
        e = by_category["database"]
        signals.needs_database = True
        signals.database_hint = f"{e.file_path}:{e.line_number} — {e.snippet}"
    if "cache" in by_category and not signals.needs_cache:
        e = by_category["cache"]
        signals.needs_cache = True
        signals.cache_hint = f"{e.file_path}:{e.line_number} — {e.snippet}"

    return signals


# ─────────────── Golden-path archetype matching (AI_AGENTIC_ORCHESTRATION_PLAN.md §2.3) ───────────────
#
# Bounds what the (future, not-yet-built) Infra Architect Agent is asked to
# generate — it parameterizes ONE of these six known-good shapes, never
# invents a topology freehand. Matching happens here, off the same real
# signals as everything else in this module (infra_signals, Dockerfile
# count, an actual Procfile process-type line) — never a guess about scale
# or traffic, which is exactly what the Requirements Form (§2.5) exists to
# ask the human instead.
ARCHETYPE_STATIC_SITE = "static_site"
ARCHETYPE_STATELESS_WEB_SERVICE = "stateless_web_service"
ARCHETYPE_WEB_SERVICE_WITH_DATABASE = "web_service_with_database"
ARCHETYPE_WEB_SERVICE_WITH_DATABASE_AND_CACHE = "web_service_with_database_and_cache"
ARCHETYPE_BACKGROUND_WORKER = "background_worker"
ARCHETYPE_MULTI_SERVICE = "multi_service"

PROCFILE_NAME = "Procfile"
COMPOSE_FILENAMES = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}


def _parse_procfile_process_types(procfile_content: str) -> set[str]:
    """
    Heroku's own Procfile convention (also what Railway's Nixpacks/Railpack
    reads, per this platform's own industry research) — one line per
    process type: `<type>: <command>`. Used ONLY to tell a "web" process
    apart from a "worker"/"clock"/queue-consumer process — a real signal a
    human wrote, never inferred from the command text itself.
    """
    types: set[str] = set()
    for raw_line in procfile_content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        process_type = line.split(":", 1)[0].strip().lower()
        if process_type:
            types.add(process_type)
    return types


def match_golden_path_archetype(
    detection: "BuildDetection",
    file_paths: list[str],
    procfile_content: str | None = None,
) -> str:
    """
    Precedence, highest first — matches §2.3's table:
      1. multi_service   — more than one Dockerfile, or a compose file present.
                            Overrides everything else since it changes the whole
                            topology shape (multiple target groups), regardless
                            of what any single service inside it needs.
      2. background_worker — a real Procfile with a non-"web" process type and
                            no "web" process type at all (a repo declaring
                            BOTH is a web app that also runs a worker process,
                            which is exactly a stateless/db web service, not a
                            pure worker).
      3. static_site      — detect_infra_signals already decided this from
                            language=="static" or framework=="spa".
      4. web_service_with_database_and_cache / _with_database / stateless —
                            purely from infra_signals' real dependency hits.
    No branch here ever fires without a concrete signal — the fallback is
    always the least-assuming archetype (stateless_web_service), never a
    guess at something fancier.
    """
    if len(_find_candidates(file_paths, DOCKERFILE_NAMES)) > 1 or _find_candidates(file_paths, COMPOSE_FILENAMES):
        return ARCHETYPE_MULTI_SERVICE

    if procfile_content:
        process_types = _parse_procfile_process_types(procfile_content)
        if process_types and "web" not in process_types:
            return ARCHETYPE_BACKGROUND_WORKER

    signals = detection.infra_signals
    if signals and signals.is_static_site:
        return ARCHETYPE_STATIC_SITE
    if signals and signals.needs_database and signals.needs_cache:
        return ARCHETYPE_WEB_SERVICE_WITH_DATABASE_AND_CACHE
    if signals and signals.needs_database:
        return ARCHETYPE_WEB_SERVICE_WITH_DATABASE

    return ARCHETYPE_STATELESS_WEB_SERVICE


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
    # Populated by the detect_build_method wrapper below, after the method/
    # language/framework decision is final — infra needs are inferred from
    # the SAME already-fetched manifest content, never a separate fetch.
    infra_signals: InfraSignals | None = None
    # One of the ARCHETYPE_* constants — see match_golden_path_archetype.
    archetype: str | None = None


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
    requirements_txt_content: str | None = None,
    procfile_content: str | None = None,
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
    `requirements_txt_content`: raw text of a found requirements.txt, if
    any — used ONLY for infra-signal inference (see `detect_infra_signals`);
    never read for build-method/language decisions, which stay existence-
    only for requirements.txt as they always have been.
    `procfile_content`: raw text of a found Procfile, if any — used ONLY for
    golden-path archetype matching (see `match_golden_path_archetype`),
    specifically to tell a "web" process apart from a "worker" one.
    """
    detection = _detect_build_method_core(
        file_paths, package_json_content, yaml_manifest_content, yaml_manifest_path
    )
    detection.infra_signals = detect_infra_signals(package_json_content, requirements_txt_content, detection)
    detection.archetype = match_golden_path_archetype(detection, file_paths, procfile_content)
    return detection


def _detect_build_method_core(
    file_paths: list[str],
    package_json_content: dict | None = None,
    yaml_manifest_content: str | None = None,
    yaml_manifest_path: str | None = None,
) -> BuildDetection:
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
