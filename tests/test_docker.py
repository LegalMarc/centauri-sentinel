"""Tests to verify Docker and deployment configuration correctness."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml


def test_entrypoint_sh_syntax() -> None:
    """Verify that docker/entrypoint.sh has valid shell syntax (sh -n)."""
    entrypoint_path = Path(__file__).parent.parent / "docker" / "entrypoint.sh"
    assert entrypoint_path.exists()

    # Run syntax check: sh -n
    res = subprocess.run(["sh", "-n", str(entrypoint_path)], capture_output=True, text=True)
    assert res.returncode == 0, f"entrypoint.sh has invalid shell syntax: {res.stderr}"


def test_dockerfile_uses_gosu_not_suexec() -> None:
    """Verify that the Dockerfile does not use Alpine's su-exec but uses Debian's gosu."""
    dockerfile_path = Path(__file__).parent.parent / "Dockerfile"
    assert dockerfile_path.exists()

    content = dockerfile_path.read_text()

    # Assert gosu is installed and used
    assert "gosu" in content.lower()
    # Assert su-exec is not present
    assert "su-exec" not in content.lower()


def test_docker_compose_yaml_syntax() -> None:
    """Verify that docker-compose.yml has valid YAML syntax."""
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_path.exists()

    with compose_path.open() as f:
        try:
            data = yaml.safe_load(f)
            assert isinstance(data, dict)
            assert "services" in data
        except yaml.YAMLError as exc:
            pytest.fail(f"docker-compose.yml has invalid YAML syntax: {exc}")
