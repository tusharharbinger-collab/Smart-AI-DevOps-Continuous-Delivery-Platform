"""
services/api-gateway/tests/test_repo_scanner_code_evidence.py

Covers shared/repo_scanner.py's Phase D deep code-evidence scan
(AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.1): select_files_for_code_scan,
scan_source_evidence, apply_code_evidence. Pure functions, no network, no AI — same convention as
test_repo_scanner.py's coverage of detect_build_method.
"""
from shared.repo_scanner import (
    CodeEvidence,
    InfraSignals,
    apply_code_evidence,
    infer_scan_language,
    scan_source_evidence,
    select_files_for_code_scan,
)


# ─────────────────────────── infer_scan_language ───────────────────────────


def test_infers_python_from_requirements_txt_even_when_a_dockerfile_exists():
    # Real bug found live: BuildDetection.language stays None whenever a Dockerfile is present (the most
    # common real case) - this must derive the scan language independently of build method.
    assert infer_scan_language(["Dockerfile", "requirements.txt", "src/app.py"]) == "python"


def test_infers_node_from_package_json():
    assert infer_scan_language(["package.json", "src/index.js"]) == "node"


def test_returns_none_when_no_recognized_manifest_exists():
    assert infer_scan_language(["README.md", "LICENSE"]) is None


def test_ignores_a_manifest_buried_too_deep():
    assert infer_scan_language(["a/b/c/d/requirements.txt"]) is None


# ─────────────────────────── select_files_for_code_scan ───────────────────────────


def test_selects_only_files_matching_the_detected_language():
    files = ["src/app.py", "src/app.js", "README.md", "src/db.py"]
    assert select_files_for_code_scan(files, "python") == ["src/app.py", "src/db.py"]


def test_returns_empty_for_an_unsupported_or_unknown_language():
    assert select_files_for_code_scan(["src/app.rb"], "ruby") == []
    assert select_files_for_code_scan(["src/app.py"], None) == []


def test_skips_vendored_generated_and_test_directories():
    files = [
        "src/app.py",
        "node_modules/pkg/index.js",
        "vendor/lib.py",
        "dist/bundle.js",
        "tests/test_app.py",
        "src/tests/test_helpers.py",
    ]
    assert select_files_for_code_scan(files, "python") == ["src/app.py"]
    assert select_files_for_code_scan(["node_modules/pkg/index.js", "src/main.js"], "node") == ["src/main.js"]


def test_caps_at_the_max_file_count():
    files = [f"src/mod_{i}.py" for i in range(50)]
    result = select_files_for_code_scan(files, "python")
    assert len(result) == 15
    assert result == files[:15]  # deterministic (tree order), not sampled


# ─────────────────────────── scan_source_evidence ───────────────────────────


def test_finds_a_real_s3_client_call_site():
    evidence = scan_source_evidence({"src/upload.py": "import boto3\ns3 = boto3.client('s3')\n"})
    assert len(evidence) == 1
    assert evidence[0] == CodeEvidence(file_path="src/upload.py", line_number=2, snippet="s3 = boto3.client('s3')", category="object_storage")


def test_finds_a_real_database_connection_call_site():
    evidence = scan_source_evidence({"src/db.py": "from sqlalchemy import create_engine\nengine = create_engine(DATABASE_URL)\n"})
    assert any(e.category == "database" and e.line_number == 2 for e in evidence)


def test_finds_a_real_redis_client_call_site():
    evidence = scan_source_evidence({"src/cache.py": "import redis\nr = redis.Redis(host='localhost')\n"})
    assert any(e.category == "cache" for e in evidence)


def test_bare_import_with_no_call_site_produces_no_evidence():
    # Importing a package proves less than a dependency name already does - only a real construction call
    # counts as evidence, matching the module's "never guess beyond a real signal" rule.
    evidence = scan_source_evidence({"src/app.py": "import boto3\nimport redis\n"})
    assert evidence == []


def test_multiple_files_and_multiple_hits_are_all_returned():
    evidence = scan_source_evidence({
        "src/db.py": "engine = create_engine(url)\n",
        "src/storage.py": "client = boto3.client('s3')\nbucket = boto3.resource('s3')\n",
    })
    assert len(evidence) == 3


def test_a_huge_file_is_truncated_not_skipped():
    huge_prefix = "x = 1\n" * 10_000  # well over MAX_CODE_SCAN_FILE_BYTES
    content = huge_prefix + "engine = create_engine(url)\n"  # lands past the truncation point
    evidence = scan_source_evidence({"src/app.py": content})
    assert evidence == []  # the real signal was past the cap - honest, not a false negative pretending otherwise
    # but evidence near the START of a large file is still found:
    content2 = "engine = create_engine(url)\n" + huge_prefix
    evidence2 = scan_source_evidence({"src/app.py": content2})
    assert len(evidence2) == 1


# ─────────────────────────── apply_code_evidence ───────────────────────────


def test_upgrades_a_signal_the_manifest_missed():
    signals = InfraSignals()  # no manifest-based database signal
    evidence = [CodeEvidence(file_path="src/db.py", line_number=3, snippet="create_engine(url)", category="database")]
    result = apply_code_evidence(signals, evidence)
    assert result.needs_database is True
    assert result.database_hint == "src/db.py:3 — create_engine(url)"
    assert result.code_evidence == evidence


def test_never_overrides_an_existing_manifest_hint():
    signals = InfraSignals(needs_database=True, database_hint="psycopg2-binary")
    evidence = [CodeEvidence(file_path="src/db.py", line_number=3, snippet="create_engine(url)", category="database")]
    result = apply_code_evidence(signals, evidence)
    assert result.needs_database is True
    assert result.database_hint == "psycopg2-binary"  # unchanged - manifest hint is primary
    assert result.code_evidence == evidence  # still recorded as corroborating evidence


def test_no_evidence_leaves_signals_completely_unchanged_except_the_empty_list():
    signals = InfraSignals(needs_cache=True, cache_hint="redis")
    result = apply_code_evidence(signals, [])
    assert result.needs_database is False
    assert result.needs_cache is True
    assert result.cache_hint == "redis"
    assert result.code_evidence == []
