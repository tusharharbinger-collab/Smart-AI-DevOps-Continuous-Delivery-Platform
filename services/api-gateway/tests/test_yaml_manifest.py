"""
services/api-gateway/tests/test_yaml_manifest.py

Covers parse_yaml_manifest and detect_build_method's priority rule: an
explicit smartcd.yaml always wins over Dockerfile/language-manifest
inference, since a human declaration is never ambiguous the way a guessed
signal is — even when it's wrong, it's wrong on purpose, not by accident.
"""
import pytest

from shared.repo_scanner import detect_build_method, parse_yaml_manifest, YamlManifestError


def test_docker_runtime_defers_to_the_repos_real_dockerfile():
    result = parse_yaml_manifest("runtime: docker\ndockerfilePath: backend/Dockerfile\ntestCommand: pytest\n")
    assert result.method == "yaml_manifest"
    assert result.dockerfile_path == "backend/Dockerfile"
    assert result.test_command == "pytest"
    assert result.confidence == "high"


def test_docker_runtime_defaults_dockerfile_path_to_repo_root():
    result = parse_yaml_manifest("runtime: docker\n")
    assert result.dockerfile_path == "Dockerfile"


def test_python_runtime_with_start_command_is_high_confidence():
    result = parse_yaml_manifest("runtime: python\nstartCommand: \"uvicorn main:app\"\ntestCommand: \"pytest tests/\"\n")
    assert result.method == "yaml_manifest"
    assert result.language == "python"
    assert result.start_command == "uvicorn main:app"
    assert result.test_command == "pytest tests/"
    assert result.confidence == "high"
    assert result.issues == []


def test_non_docker_runtime_missing_start_command_is_flagged():
    result = parse_yaml_manifest("runtime: python\n")
    assert result.confidence == "low"
    assert any("startCommand" in issue for issue in result.issues)


def test_unsupported_synthesis_language_is_flagged_but_still_parsed():
    result = parse_yaml_manifest("runtime: rust\nstartCommand: \"./target/release/app\"\n")
    assert result.language == "rust"
    assert result.confidence == "low"
    assert any("no Dockerfile-synthesis template" in issue for issue in result.issues)


def test_deploy_block_is_carried_forward_but_not_interpreted():
    result = parse_yaml_manifest("runtime: docker\ndeploy:\n  target: ecs\n  region: us-east-1\n")
    assert result.deploy_config == {"target": "ecs", "region": "us-east-1"}


def test_missing_runtime_field_raises_clear_error():
    with pytest.raises(YamlManifestError, match="runtime"):
        parse_yaml_manifest("startCommand: foo\n")


def test_malformed_yaml_raises_clear_error_not_a_crash():
    with pytest.raises(YamlManifestError, match="not valid YAML"):
        parse_yaml_manifest("runtime: [unclosed\n")


def test_non_mapping_yaml_raises_clear_error():
    with pytest.raises(YamlManifestError, match="mapping"):
        parse_yaml_manifest("- just\n- a\n- list\n")


def test_detect_build_method_prefers_yaml_manifest_over_a_real_dockerfile():
    """The core priority rule: even with a Dockerfile AND a language
    manifest both present, an explicit smartcd.yaml wins — it's what the
    human actually told us to do."""
    result = detect_build_method(
        ["Dockerfile", "requirements.txt", "smartcd.yaml"],
        yaml_manifest_content="runtime: python\nstartCommand: \"python app.py\"\n",
    )
    assert result.method == "yaml_manifest"
    assert result.language == "python"


def test_detect_build_method_falls_back_to_inference_when_no_manifest_present():
    result = detect_build_method(["Dockerfile"], yaml_manifest_content=None)
    assert result.method == "dockerfile"


def test_detect_build_method_reports_a_malformed_manifest_as_unsupported_not_silent_fallback():
    """Real gap this guards against: a typo'd smartcd.yaml must be reported
    clearly, never silently ignored in favor of whatever Dockerfile/manifest
    happens to also exist — that would produce a DIFFERENT build than the
    one the human's (broken) manifest asked for, with no explanation why."""
    result = detect_build_method(
        ["Dockerfile", "smartcd.yaml"],
        yaml_manifest_content="not: valid\nrandom: keys\n",
    )
    assert result.method == "unsupported"
    assert "runtime" in result.issues[0]


def test_detect_build_method_resolves_manifest_path_for_a_subfolder_yaml_manifest():
    """Real gap found live: a smartcd.yaml in a subfolder (e.g.
    nodocker/smartcd.yaml) declaring a language runtime never carries its
    own location — parse_yaml_manifest only sees the raw YAML text — so
    manifest_path was left unset and every downstream synthesis defaulted
    to looking for the manifest at repo ROOT instead of next to the yaml
    file, failing a build that works fine through a real pipeline once the
    path is corrected by hand. This is exactly the shape of the real
    tusharharbinger-collab/test- repo's nodocker/smartcd.yaml."""
    result = detect_build_method(
        ["nodocker/smartcd.yaml", "nodocker/requirements.txt", "nodocker/main.py"],
        yaml_manifest_content="runtime: python\nstartCommand: \"python main.py\"\n",
        yaml_manifest_path="nodocker/smartcd.yaml",
    )
    assert result.method == "yaml_manifest"
    assert result.manifest_path == "nodocker/requirements.txt"
    assert result.confidence == "high"
    assert result.issues == []


def test_detect_build_method_resolves_manifest_path_at_repo_root():
    result = detect_build_method(
        ["smartcd.yaml", "requirements.txt"],
        yaml_manifest_content="runtime: python\nstartCommand: \"python main.py\"\n",
        yaml_manifest_path="smartcd.yaml",
    )
    assert result.manifest_path == "requirements.txt"


def test_detect_build_method_flags_missing_manifest_file_instead_of_guessing():
    """No requirements.txt/Pipfile/pyproject.toml actually exists next to
    the yaml manifest — this must be reported as a real, verifiable gap
    (confidence downgraded, a concrete issue) rather than silently defaulting
    manifest_path to a filename that isn't really there."""
    result = detect_build_method(
        ["nodocker/smartcd.yaml", "nodocker/main.py"],
        yaml_manifest_content="runtime: python\nstartCommand: \"python main.py\"\n",
        yaml_manifest_path="nodocker/smartcd.yaml",
    )
    assert result.manifest_path is None
    assert result.confidence == "low"
    assert any("requirements.txt" in issue for issue in result.issues)


def test_detect_build_method_does_not_touch_manifest_path_for_docker_runtime():
    result = detect_build_method(
        ["nodocker/smartcd.yaml", "nodocker/Dockerfile"],
        yaml_manifest_content="runtime: docker\n",
        yaml_manifest_path="nodocker/smartcd.yaml",
    )
    assert result.manifest_path is None
    assert result.dockerfile_path == "Dockerfile"
