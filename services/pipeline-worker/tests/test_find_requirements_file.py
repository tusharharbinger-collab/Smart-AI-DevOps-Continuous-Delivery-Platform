"""
services/pipeline-worker/tests/test_find_requirements_file.py

Real bug found live testing the build-preview feature against a repo that
legitimately has two requirements.txt files at equal depth (two
independent services in one repo): the old "shallowest wins" heuristic
picked whichever glob happened to return first, silently installing the
WRONG service's dependencies for this run's tests — the failure it
produced (a plain ModuleNotFoundError) gave no hint the wrong file had
been used at all. `preferred_subdir` — the same folder the build stage
already resolved its Dockerfile/manifest to — removes the ambiguity.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.tasks.build_task import _find_requirements_file


def test_preferred_subdir_wins_even_when_another_requirements_file_is_shallower(tmp_path):
    # Reproduces the real repo shape this was found against: two services,
    # "test/requirements.txt" and "nodocker/requirements.txt", equal depth.
    os.makedirs(tmp_path / "test")
    os.makedirs(tmp_path / "nodocker")
    (tmp_path / "test" / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "nodocker" / "requirements.txt").write_text("flask\n")

    result = _find_requirements_file(str(tmp_path), preferred_subdir="nodocker")

    assert result == os.path.join(str(tmp_path), "nodocker", "requirements.txt")


def test_no_preferred_subdir_falls_back_to_shallowest_match(tmp_path):
    os.makedirs(tmp_path / "a" / "b")
    (tmp_path / "requirements.txt").write_text("root\n")
    (tmp_path / "a" / "b" / "requirements.txt").write_text("nested\n")

    result = _find_requirements_file(str(tmp_path))

    assert result == os.path.join(str(tmp_path), "requirements.txt")


def test_preferred_subdir_with_no_requirements_file_there_falls_back(tmp_path):
    os.makedirs(tmp_path / "empty-folder")
    (tmp_path / "requirements.txt").write_text("root\n")

    result = _find_requirements_file(str(tmp_path), preferred_subdir="empty-folder")

    assert result == os.path.join(str(tmp_path), "requirements.txt")


def test_no_requirements_file_anywhere_returns_none(tmp_path):
    assert _find_requirements_file(str(tmp_path)) is None
