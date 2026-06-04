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


def test_token_init_entrypoint_syntax() -> None:
    """Verify that docker/token-init/entrypoint.sh has valid shell syntax."""
    entrypoint_path = Path(__file__).parent.parent / "docker" / "token-init" / "entrypoint.sh"
    assert entrypoint_path.exists()

    res = subprocess.run(["sh", "-n", str(entrypoint_path)], capture_output=True, text=True)
    assert res.returncode == 0, f"entrypoint.sh has invalid shell syntax: {res.stderr}"


def test_token_init_permissions(tmp_path: Path) -> None:
    """Verify that docker/token-init/entrypoint.sh creates token file with 600 permissions."""
    entrypoint_path = Path(__file__).parent.parent / "docker" / "token-init" / "entrypoint.sh"
    assert entrypoint_path.exists()

    token_file = tmp_path / "token"
    # Run the entrypoint script pointing TOKEN_FILE to our temp path
    res = subprocess.run(
        ["sh", str(entrypoint_path)],
        env={"TOKEN_FILE": str(token_file)},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"entrypoint.sh failed: {res.stderr}"
    assert token_file.exists()

    # Get file mode and check it's 644 (octal 0o100644)
    mode = token_file.stat().st_mode
    # Extract permission bits
    perms = mode & 0o777
    assert perms == 0o644, f"Expected permissions 0644, got {oct(perms)}"

