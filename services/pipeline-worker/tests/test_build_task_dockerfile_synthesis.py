"""
services/pipeline-worker/tests/test_build_task_dockerfile_synthesis.py

Confirms run_build_task's `dockerfile_content` parameter actually writes
the synthesized Dockerfile into the resolved build context BEFORE the
(mocked here) `docker build` call — the mechanism that lets a repo with no
Dockerfile of its own still build through the exact same code path as one
that has a real one.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.tasks.build_task as build_task_module
from src.tasks.build_task import run_build_task


def test_synthesized_dockerfile_is_written_before_build_is_called(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("backend", exist_ok=True)
    with open("backend/requirements.txt", "w", encoding="utf-8") as f:
        f.write("fastapi\n")

    fake_client = MagicMock()
    monkeypatch.setattr(build_task_module.docker, "from_env", lambda: fake_client)

    dockerfile_text = "FROM python:3.11-slim\nCMD [\"python\", \"app.py\"]\n"
    result = run_build_task(
        "preview-run-1",
        dockerfile_path="backend/Dockerfile",
        image_tag="preview",
        dockerfile_content=dockerfile_text,
    )

    written_path = os.path.join("backend", "Dockerfile")
    assert os.path.exists(written_path)
    with open(written_path, encoding="utf-8") as f:
        assert f.read() == dockerfile_text

    fake_client.images.build.assert_called_once()
    call_kwargs = fake_client.images.build.call_args.kwargs
    assert call_kwargs["dockerfile"] == "Dockerfile"
    assert call_kwargs["path"] == os.path.join(".", "backend")
    assert result["status"] == "success"


def test_no_dockerfile_content_leaves_existing_real_dockerfile_untouched(tmp_path, monkeypatch):
    """Regression guard: a real, human-authored Dockerfile must never be
    silently overwritten just because this parameter exists."""
    monkeypatch.chdir(tmp_path)
    with open("Dockerfile", "w", encoding="utf-8") as f:
        f.write("FROM real-human-authored-image\n")

    fake_client = MagicMock()
    monkeypatch.setattr(build_task_module.docker, "from_env", lambda: fake_client)

    run_build_task("preview-run-2", dockerfile_path="Dockerfile", image_tag="preview")

    with open("Dockerfile", encoding="utf-8") as f:
        assert f.read() == "FROM real-human-authored-image\n"
