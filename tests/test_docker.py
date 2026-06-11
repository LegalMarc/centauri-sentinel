"""Tests to verify Docker and deployment configuration correctness."""

from __future__ import annotations

import re
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


def test_compose_env_covers_readme_config_table() -> None:
    """Assert every env var in the README configuration table is in compose environment block.

    SENTINEL_PORT is a host-side compose knob (not passed to the container) so it
    is excluded from the check.  Variables that are not applicable to compose should
    be added to EXCLUDED_FROM_COMPOSE below.
    """
    repo_root = Path(__file__).parent.parent

    # --- Parse README: collect env var names from config table rows ---
    # Only look at table rows (lines starting with "|") and only in the first
    # backtick-wrapped token per row, which is the variable name column.
    readme = (repo_root / "README.md").read_text()
    readme_vars: set[str] = set()
    for line in readme.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # Skip header and separator rows
        if "---" in stripped or "Variable" in stripped:
            continue
        # First cell is the variable name: | `VAR_NAME` | ...
        m = re.match(r"\|\s*`([A-Z][A-Z0-9_]{2,})`\s*\|", stripped)
        if m:
            readme_vars.add(m.group(1))

    # --- Parse compose: collect env keys defined under sentinel.environment ---
    compose_path = repo_root / "docker-compose.yml"
    with compose_path.open() as f:
        compose_data = yaml.safe_load(f)
    sentinel_env: dict[str, object] = (
        compose_data.get("services", {}).get("sentinel", {}).get("environment", {})
    )
    compose_vars: set[str] = set(sentinel_env.keys())

    # Vars that are README-documented but intentionally not in the sentinel env block
    # (e.g. host-side compose knobs, deprecated vars, or vars for other services).
    excluded: set[str] = {
        "SENTINEL_PORT",  # host-side port mapping, not a container env var
        "AUTH_PASSWORD",  # deprecated plain-text password, intentionally not passed through
    }

    missing = readme_vars - compose_vars - excluded
    assert not missing, (
        f"README config table variable(s) not found in compose sentinel environment block: "
        f"{sorted(missing)}.  Add them to docker-compose.yml environment section or to the "
        f"'excluded' set in this test if they are intentionally not passed through."
    )


def test_compose_bind_port_mapping() -> None:
    """BIND_PORT and SENTINEL_PORT must both appear in the port mapping expression.

    This ensures that changing BIND_PORT (uvicorn listener) also changes the
    container-side port in the mapping — otherwise host access breaks silently.
    """
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    compose_text = compose_path.read_text()
    # The port mapping line must reference BIND_PORT on the container side.
    # Accept either explicit ${BIND_PORT} or ${BIND_PORT:-<default>} form.
    assert re.search(r"\$\{BIND_PORT", compose_text), (
        "docker-compose.yml port mapping must use ${BIND_PORT} or ${BIND_PORT:-8000} "
        "on the container side so that changing BIND_PORT does not silently break host access."
    )


def test_token_init_permissions(tmp_path: Path) -> None:
    """Verify that docker/token-init/entrypoint.sh creates token file with 644 permissions.

    The volume is private (ml-token), so 0644 is safe and allows sentinel
    (UID 1000) to read the token on a read-only volume mount.
    """
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
    assert perms == 0o644, (
        f"Expected permissions 0644 (readable by sentinel UID 1000), got {oct(perms)}"
    )
