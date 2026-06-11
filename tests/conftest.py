from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _set_test_env() -> None:
    os.environ["PRINTER_ACCESS_CODE"] = "123456"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
