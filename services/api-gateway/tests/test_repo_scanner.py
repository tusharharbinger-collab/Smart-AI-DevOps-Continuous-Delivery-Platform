"""
services/api-gateway/tests/test_repo_scanner.py

Covers shared/repo_scanner.py's detect_build_method — pure function, no
network, no AI. This is the piece the "100% surety we produce a build
unless it's a real human-side gap" guarantee actually rests on, so every
branch (Dockerfile found, each supported language with/without a real
start-command signal, and genuinely unsupported) gets a direct test.
"""
from shared.repo_scanner import detect_build_method, suggest_networking_defaults


def test_dockerfile_at_repo_root_is_high_confidence():
    result = detect_build_method(["Dockerfile", "main.py", "requirements.txt"])
    assert result.method == "dockerfile"
    assert result.dockerfile_path == "Dockerfile"
    assert result.confidence == "high"


def test_dockerfile_in_a_shallow_subfolder_is_found():
    result = detect_build_method(["test/Dockerfile", "test/main.py", "test/requirements.txt"])
    assert result.method == "dockerfile"
    assert result.dockerfile_path == "test/Dockerfile"


def test_dockerfile_deeply_nested_is_not_treated_as_a_candidate():
    # Matches this repo's own real MAX_SEARCH_DEPTH rationale: a Dockerfile
    # buried inside an unrelated vendored dependency several folders deep
    # is noise, not a real build entrypoint.
    result = detect_build_method(["a/b/c/d/Dockerfile", "requirements.txt"])
    assert result.method == "synthesized"
    assert result.language == "python"


def test_dockerfile_takes_priority_over_a_coexisting_manifest():
    result = detect_build_method(["Dockerfile", "requirements.txt", "package.json"])
    assert result.method == "dockerfile"


def test_python_manifest_with_no_dockerfile_is_synthesized_low_confidence():
    result = detect_build_method(["requirements.txt", "main.py"])
    assert result.method == "synthesized"
    assert result.language == "python"
    assert result.manifest_path == "requirements.txt"
    assert result.start_command is None
    assert result.confidence == "low"
    assert any("start command" in issue for issue in result.issues)


def test_node_manifest_with_real_start_script_is_high_confidence():
    result = detect_build_method(
        ["package.json", "index.js"],
        package_json_content={"scripts": {"start": "node index.js"}},
    )
    assert result.method == "synthesized"
    assert result.language == "node"
    assert result.start_command == "npm start"
    assert result.confidence == "high"
    assert result.issues == []


def test_node_manifest_without_start_script_is_low_confidence():
    result = detect_build_method(["package.json", "index.js"], package_json_content={"scripts": {}})
    assert result.method == "synthesized"
    assert result.start_command is None
    assert result.confidence == "low"


def test_node_start_command_never_guessed_from_absent_package_json_content():
    """Real gap this guards against: if the caller never fetched
    package.json's content (e.g. a transient GitHub API failure fetching
    just that one file), this must NOT silently assume no start script —
    it already correctly has no signal, so start_command stays None either
    way, but this pins the behavior so a future change can't start
    guessing "npm start" as a default."""
    result = detect_build_method(["package.json", "index.js"], package_json_content=None)
    assert result.start_command is None
    assert result.confidence == "low"


def test_go_manifest_detected():
    result = detect_build_method(["go.mod", "main.go"])
    assert result.method == "synthesized"
    assert result.language == "go"


# ─────────────── Guaranteed Live Web App CI/CD — framework detection ───────────────


def test_plain_node_server_with_no_framework_signal_is_node_server():
    result = detect_build_method(
        ["package.json", "index.js"],
        package_json_content={"dependencies": {"express": "^4.0.0"}, "scripts": {"start": "node index.js"}},
    )
    assert result.language == "node"
    assert result.framework == "node-server"
    assert result.confidence == "high"


def test_vite_react_app_is_detected_as_spa_and_needs_no_start_command():
    result = detect_build_method(
        ["package.json", "index.html", "src/main.tsx"],
        package_json_content={
            "devDependencies": {"vite": "^5.0.0"},
            "dependencies": {"react": "^18.0.0"},
            "scripts": {"build": "vite build"},
        },
    )
    assert result.language == "node"
    assert result.framework == "spa"
    assert result.start_command is None
    assert result.confidence == "high"
    assert result.issues == []


def test_create_react_app_react_scripts_is_also_detected_as_spa():
    result = detect_build_method(
        ["package.json"],
        package_json_content={"dependencies": {"react-scripts": "^5.0.0"}, "scripts": {"build": "react-scripts build"}},
    )
    assert result.framework == "spa"


def test_spa_without_a_build_script_is_low_confidence_with_a_real_issue():
    result = detect_build_method(
        ["package.json"], package_json_content={"devDependencies": {"vite": "^5.0.0"}, "scripts": {}}
    )
    assert result.framework == "spa"
    assert result.confidence == "low"
    assert any("scripts.build" in issue for issue in result.issues)


def test_nextjs_app_is_detected_and_still_needs_a_start_command():
    result = detect_build_method(
        ["package.json"],
        package_json_content={
            "dependencies": {"next": "^14.0.0", "react": "^18.0.0"},
            "scripts": {"start": "next start", "build": "next build"},
        },
    )
    assert result.language == "node"
    assert result.framework == "nextjs"
    assert result.start_command == "npm start"
    assert result.confidence == "high"


def test_nextjs_takes_priority_over_a_coexisting_vite_devdependency():
    # Real-world case: a Next.js app that also has vite in devDependencies
    # for an unrelated tool. Next's own dependency is the more specific,
    # unambiguous signal.
    result = detect_build_method(
        ["package.json"],
        package_json_content={
            "dependencies": {"next": "^14.0.0"},
            "devDependencies": {"vite": "^5.0.0"},
            "scripts": {"start": "next start"},
        },
    )
    assert result.framework == "nextjs"


# ─────────────── Guaranteed Live Web App CI/CD — static HTML detection ───────────────


def test_bare_static_site_with_index_html_and_no_manifest_is_detected():
    result = detect_build_method(["index.html", "styles.css", "app.js"])
    assert result.method == "synthesized"
    assert result.language == "static"
    assert result.manifest_path == "index.html"
    assert result.confidence == "high"
    assert result.issues == []


def test_static_site_index_html_in_a_shallow_subfolder_is_found():
    result = detect_build_method(["public/index.html", "public/app.js"])
    assert result.language == "static"
    assert result.manifest_path == "public/index.html"


def test_a_node_project_is_never_misdetected_as_static_even_with_an_index_html_present():
    # package.json must win — index.html only kicks in once no recognized
    # language manifest matched at all.
    result = detect_build_method(
        ["package.json", "index.html"], package_json_content={"scripts": {"start": "node server.js"}}
    )
    assert result.language == "node"


# ─────────────── Guaranteed Live Web App CI/CD — networking defaults ───────────────
#
# Real gap this closes: an ECS target group health check hits health_check_path
# DIRECTLY, bypassing the ALB's own path-prefix routing entirely — the
# wizard's flat "/healthz" + 8080 default meant ECS killed a real web app's
# task repeatedly for "failing health checks" since almost no arbitrary web
# app implements that literal route.


def test_static_site_suggests_root_path_and_port_80():
    detection = detect_build_method(["index.html"])
    suggestion = suggest_networking_defaults(detection)
    assert suggestion == {"suggested_health_check_path": "/", "suggested_port": 80}


def test_spa_suggests_root_path_and_port_80():
    detection = detect_build_method(
        ["package.json"], package_json_content={"devDependencies": {"vite": "^5.0.0"}, "scripts": {"build": "vite build"}}
    )
    suggestion = suggest_networking_defaults(detection)
    assert suggestion == {"suggested_health_check_path": "/", "suggested_port": 80}


def test_nextjs_suggests_root_path_and_port_3000():
    detection = detect_build_method(
        ["package.json"],
        package_json_content={"dependencies": {"next": "^14.0.0"}, "scripts": {"start": "next start", "build": "next build"}},
    )
    suggestion = suggest_networking_defaults(detection)
    assert suggestion == {"suggested_health_check_path": "/", "suggested_port": 3000}


def test_plain_node_server_keeps_the_healthz_default():
    detection = detect_build_method(
        ["package.json"], package_json_content={"scripts": {"start": "node index.js"}}
    )
    suggestion = suggest_networking_defaults(detection)
    assert suggestion == {"suggested_health_check_path": "/healthz", "suggested_port": 8080}


def test_python_and_dockerfile_projects_also_keep_the_healthz_default():
    assert suggest_networking_defaults(detect_build_method(["requirements.txt", "main.py"])) == {
        "suggested_health_check_path": "/healthz", "suggested_port": 8080,
    }
    assert suggest_networking_defaults(detect_build_method(["Dockerfile"])) == {
        "suggested_health_check_path": "/healthz", "suggested_port": 8080,
    }


def test_test_config_detected_via_known_file():
    result = detect_build_method(["requirements.txt", "pytest.ini", "main.py"])
    assert result.test_config_found is True


def test_test_config_detected_via_tests_folder_convention():
    result = detect_build_method(["requirements.txt", "tests/test_main.py"])
    assert result.test_config_found is True


def test_no_test_signal_at_all():
    result = detect_build_method(["requirements.txt", "main.py"])
    assert result.test_config_found is False


def test_genuinely_unsupported_repo_reports_a_clear_actionable_reason():
    result = detect_build_method(["README.md", "LICENSE"])
    assert result.method == "unsupported"
    assert result.confidence == "high"
    assert len(result.issues) == 1
    assert "Dockerfile" in result.issues[0]
    assert "requirements.txt" in result.issues[0]


def test_empty_repo_is_unsupported_not_a_crash():
    result = detect_build_method([])
    assert result.method == "unsupported"
