"""
services/pipeline-worker/tests/test_dockerfile_synthesis.py

Covers synthesize_dockerfile — the other half of the "no Dockerfile in the
repo" build path, alongside shared/repo_scanner.py's detection. Real
correctness bar: the generated Dockerfile must actually be valid enough
for `docker build` to succeed, not just non-empty text — a live end-to-end
test exercises that separately; this pins the exact content contract.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from src.tasks.dockerfile_synthesis import synthesize_dockerfile, UnsupportedLanguageError


def test_python_template_installs_requirements_and_runs_start_command():
    result = synthesize_dockerfile("python", "requirements.txt", "uvicorn main:app --host 0.0.0.0")
    assert "FROM python:3.11-slim" in result
    assert "COPY requirements.txt ." in result
    assert "RUN pip install --no-cache-dir -r requirements.txt" in result
    assert 'CMD ["uvicorn", "main:app", "--host", "0.0.0.0"]' in result


def test_node_template_uses_npm_install_and_start_script():
    # Real bug fixed (Guaranteed Live Web App CI/CD): this used to run
    # `npm install --omit=dev`, which strips the exact tools (vite, tsc,
    # next) any real frontend build needs — devDependencies must be
    # installed for a synthesized Node build to work at all.
    result = synthesize_dockerfile("node", "package.json", "npm start")
    assert "FROM node:20-slim" in result
    assert "RUN npm install" in result
    assert "--omit=dev" not in result
    assert 'CMD ["npm", "start"]' in result


def test_go_template_is_multi_stage_and_produces_a_slim_final_image():
    result = synthesize_dockerfile("go", "go.mod", "./server")
    assert "FROM golang:1.22 AS build" in result
    assert "FROM debian:bookworm-slim" in result
    assert "COPY --from=build" in result


def test_manifest_path_in_a_subfolder_uses_just_the_filename():
    """The synthesized Dockerfile is written into the SAME folder as the
    manifest (matching build_task.py's existing context = dirname(Dockerfile)
    behavior), so COPY must reference the bare filename, not a path that
    would escape that build context."""
    result = synthesize_dockerfile("python", "backend/requirements.txt", "python app.py")
    assert "COPY requirements.txt ." in result
    assert "backend/" not in result


def test_unsupported_language_raises_a_clear_error_not_a_kind_error():
    with pytest.raises(UnsupportedLanguageError, match="java-maven"):
        synthesize_dockerfile("java-maven", "pom.xml", "java -jar app.jar")


# ─────────────── Guaranteed Live Web App CI/CD — SPA/Next.js/static ───────────────


def test_spa_template_is_multi_stage_and_builds_before_serving_with_nginx():
    result = synthesize_dockerfile("node", "package.json", None, framework="spa")
    assert "FROM node:20-slim AS build" in result
    # The real bug this avoids: --omit=dev would strip vite/tsc/etc.
    assert "RUN npm install" in result
    assert "--omit=dev" not in result
    assert "RUN npm run build" in result
    assert "FROM nginx:alpine" in result
    # Copies whichever real output folder the build actually produced
    # (Vite's dist/ or CRA's build/) rather than guessing one ahead of time.
    assert "cp -r dist/. /out/" in result
    assert "cp -r build/. /out/" in result
    assert "COPY --from=build /out /usr/share/nginx/html" in result
    # No runtime CMD at all — nginx's own image entrypoint serves the files.
    assert "CMD" not in result


def test_spa_template_writes_an_spa_fallback_nginx_config():
    result = synthesize_dockerfile("node", "package.json", None, framework="spa")
    assert "try_files $uri $uri/ /index.html" in result
    assert "/etc/nginx/templates/default.conf.template" in result
    assert "/docker-entrypoint.d/40-path-prefix.sh" in result
    assert "chmod +x /docker-entrypoint.d/40-path-prefix.sh" in result
    assert "envsubst" in result
    assert "${PATH_PREFIX}" in result
    # The RUN instruction writing the template and entrypoint script must be
    # a physically valid single-line RUN instruction in the synthesized Dockerfile.
    run_lines = [line for line in result.splitlines() if "default.conf.template" in line]
    assert len(run_lines) == 1


def test_nextjs_template_is_multi_stage_and_runs_a_real_start_command():
    result = synthesize_dockerfile("node", "package.json", "npm start", framework="nextjs")
    assert "FROM node:20-slim AS build" in result
    assert "RUN npm run build" in result
    assert "EXPOSE 3000" in result
    assert 'CMD ["npm", "start"]' in result
    # Runs the built app directly, no nginx involved (Next.js exec's a real
    # Node process, unlike a static SPA).
    assert "nginx" not in result
    assert "PATH_PREFIX" not in result
    assert "envsubst" not in result
    assert "40-path-prefix.sh" not in result


def test_unrecognized_node_framework_raises_a_clear_error():
    with pytest.raises(UnsupportedLanguageError, match="weird-framework"):
        synthesize_dockerfile("node", "package.json", "npm start", framework="weird-framework")


def test_node_with_no_framework_given_defaults_to_node_server():
    result = synthesize_dockerfile("node", "package.json", "npm start")
    assert "FROM node:20-slim" in result
    assert "AS build" not in result  # single-stage, matches original behavior
    assert 'CMD ["npm", "start"]' in result


def test_static_template_copies_the_detected_root_and_serves_with_nginx():
    result = synthesize_dockerfile("static", "index.html", None)
    assert "FROM nginx:alpine" in result
    assert "COPY . /usr/share/nginx/html" in result
    assert "/etc/nginx/templates/default.conf.template" in result
    assert "/docker-entrypoint.d/40-path-prefix.sh" in result
    assert "try_files $uri $uri/ /index.html" in result
    assert "EXPOSE 80" in result


def test_static_template_copies_only_the_subfolder_containing_index_html():
    result = synthesize_dockerfile("static", "public/index.html", None)
    assert "COPY public /usr/share/nginx/html" in result


def test_entrypoint_script_substitution_behavior(tmp_path):
    """
    Actually executes the entrypoint script substitution logic against a
    fixture template for both empty and real PATH_PREFIX, asserting the
    rendered nginx config text is exactly right for both cases.
    Runs via local envsubst/sh if available, or via docker nginx:alpine image.
    """
    import shutil
    import subprocess
    from src.tasks.dockerfile_synthesis import _NGINX_TEMPLATE_CONTENT, _ENTRYPOINT_SCRIPT_CONTENT

    # Extract clean template and script strings unescaping literal backslash-n
    template_str = _NGINX_TEMPLATE_CONTENT.replace("\\n", "\n")
    script_str = _ENTRYPOINT_SCRIPT_CONTENT.replace("\\n", "\n")

    def run_substitution(prefix: str | None) -> str:
        if shutil.which("envsubst") and shutil.which("sh"):
            template_file = tmp_path / "default.conf.template"
            template_file.write_text(template_str, encoding="utf-8")
            script_file = tmp_path / "40-path-prefix.sh"
            script_file.write_text(script_str, encoding="utf-8")
            conf_dir = tmp_path / "conf.d"
            conf_dir.mkdir(exist_ok=True)
            
            env = os.environ.copy()
            if prefix is not None:
                env["PATH_PREFIX"] = prefix
            else:
                env.pop("PATH_PREFIX", None)
            # On Windows, Git Bash's MSYS runtime auto-converts any value that
            # looks like a POSIX absolute path (e.g. a bare "/" or "/api/v1/..."
            # produced by the script itself) into a Windows filesystem path
            # before `sh`/`envsubst` ever see it — corrupting PATH_PREFIX with
            # something like "C:/Program Files/Git/api/v1/...". Real production
            # runs this inside a real Linux container (no MSYS involved), so
            # this is purely a local-test-harness artifact; disabling MSYS path
            # conversion for this one subprocess makes the local run match real
            # container behavior instead of silently testing the wrong thing.
            env["MSYS_NO_PATHCONV"] = "1"
            env["MSYS2_ARG_CONV_EXCL"] = "*"

            # Run via sh, overriding template and output paths for the test sandbox
            cmd = (
                f'sed -e "s|/etc/nginx/templates/default.conf.template|{template_file.as_posix()}|g" '
                f'-e "s|/etc/nginx/conf.d/default.conf|{(conf_dir / "default.conf").as_posix()}|g" '
                f'{script_file.as_posix()} | sh'
            )
            subprocess.run(["sh", "-c", cmd], env=env, check=True)
            return (conf_dir / "default.conf").read_text(encoding="utf-8")
        elif shutil.which("docker"):
            env_args = ["-e", f"PATH_PREFIX={prefix}"] if prefix is not None else []
            # In docker container, run the script against the template
            cmd = (
                f"mkdir -p /etc/nginx/templates /docker-entrypoint.d && "
                f"printf '{_NGINX_TEMPLATE_CONTENT}' > /etc/nginx/templates/default.conf.template && "
                f"printf '{_ENTRYPOINT_SCRIPT_CONTENT}' > /docker-entrypoint.d/40-path-prefix.sh && "
                f"chmod +x /docker-entrypoint.d/40-path-prefix.sh && "
                f"/docker-entrypoint.d/40-path-prefix.sh && "
                f"cat /etc/nginx/conf.d/default.conf"
            )
            proc = subprocess.run(
                ["docker", "run", "--rm", *env_args, "nginx:alpine", "sh", "-c", cmd],
                capture_output=True,
                text=True,
                check=True,
            )
            return proc.stdout
        else:
            pytest.skip("Neither envsubst nor docker available to test entrypoint script execution")

    # Case 1: Empty prefix -> standard root location
    output_empty = run_substitution("")
    assert "location / {" in output_empty
    assert "alias /usr/share/nginx/html/;" not in output_empty
    assert "try_files $uri $uri/ /index.html;" in output_empty

    # Case 2: Real prefix -> prefix location with alias
    output_real = run_substitution("/api/v1/my-app")
    assert "location /api/v1/my-app/ {" in output_real
    assert "alias /usr/share/nginx/html/;" in output_real
    assert "try_files $uri $uri/ /index.html;" in output_real

