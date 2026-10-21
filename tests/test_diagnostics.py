"""Tests for sentinel/diagnostics.py (the mqtt-slots client-slot probe).

A fake broker stands in for the printer: it accepts a fixed number of distinct
client registrations, then answers "too many clients", mirroring the firmware
behaviour the tool is meant to measure. No real MQTT connection is made.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiomqtt
import pytest

from sentinel import diagnostics
from sentinel.config import Settings

_SETTINGS = Settings(printer_ip="10.0.0.1", printer_access_code="secret")
_SERIAL = "F0113TEST"


def _message(topic: str, payload: dict[str, Any]) -> MagicMock:
    msg = MagicMock()
    msg.topic = topic
    msg.payload = json.dumps(payload).encode()
    return msg


class _FakeBroker:
    """Printer stand-in shared by every session opened through ``aiomqtt.Client``."""

    def __init__(
        self,
        *,
        capacity: int = 3,
        accept_reused_ids: bool = True,
        free_after_rejections: int | None = None,
        register_error: str | None = None,
        drop_register: bool = False,
        connect_error: Exception | None = None,
        print_state: str | None = "standby",
        start_print_after_rejections: int | None = None,
        malformed_register: bool = False,
    ) -> None:
        self.capacity = capacity
        self.accept_reused_ids = accept_reused_ids
        self.free_after_rejections = free_after_rejections
        self.register_error = register_error
        self.drop_register = drop_register
        self.connect_error = connect_error
        # print_state=None makes status pushes carry no print_status block at all.
        self.print_state = print_state
        self.start_print_after_rejections = start_print_after_rejections
        self.malformed_register = malformed_register
        self.slots: set[str] = set()
        self.rejections = 0
        self.sessions = 0
        self.disconnects = 0
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.identifiers: list[str] = []
        self.status_sessions: list[_FakeSession] = []

    def client(self, **kwargs: Any) -> _FakeSession:
        self.identifiers.append(kwargs["identifier"])
        return _FakeSession(self)

    def register_answer(self, client_id: str) -> str:
        if self.register_error is not None:
            return self.register_error
        if client_id in self.slots:
            return "ok" if self.accept_reused_ids else "fail"
        if len(self.slots) >= self.capacity:
            self.rejections += 1
            if (
                self.start_print_after_rejections is not None
                and self.rejections >= self.start_print_after_rejections
            ):
                self.print_state = "printing"  # someone starts a print mid-wait
                for session in self.status_sessions:  # the printer pushes the change
                    session.push_state()
            if (
                self.free_after_rejections is not None
                and self.rejections >= self.free_after_rejections
            ):
                self.slots.clear()  # the registrations expired
            return "too many clients"
        self.slots.add(client_id)
        return "ok"


class _FakeSession:
    def __init__(self, broker: _FakeBroker) -> None:
        self._broker = broker
        self._queue: list[MagicMock] = []
        self.subscribed: list[str] = []

    async def __aenter__(self) -> _FakeSession:
        if self._broker.connect_error is not None:
            raise self._broker.connect_error
        self._broker.sessions += 1
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self._broker.disconnects += 1
        return False

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)
        if topic == "elegoo/+/api_status":
            self._broker.status_sessions.append(self)
            self.push_state()

    def push_state(self) -> None:
        result: dict[str, Any] = {}
        if self._broker.print_state is not None:
            result["print_status"] = {"state": self._broker.print_state}
        self._queue.append(
            _message(f"elegoo/{_SERIAL}/api_status", {"method": 6000, "result": result})
        )

    async def publish(self, topic: str, payload_str: str) -> None:
        payload = json.loads(payload_str)
        self._broker.published.append((topic, payload))
        if topic.endswith("/api_register") and self._broker.malformed_register:
            sn, request_id = topic.split("/")[1], payload["request_id"]
            bad = MagicMock()
            bad.topic = f"elegoo/{sn}/{request_id}/register_response"
            bad.payload = b"not json"
            self._queue.append(bad)
        elif topic.endswith("/api_register") and not self._broker.drop_register:
            sn, request_id = topic.split("/")[1], payload["request_id"]
            self._queue.append(
                _message(
                    f"elegoo/{sn}/{request_id}/register_response",
                    {
                        "client_id": payload["client_id"],
                        "error": self._broker.register_answer(payload["client_id"]),
                    },
                )
            )

    @property
    def messages(self) -> _FakeSession:
        return self

    def __aiter__(self) -> _FakeSession:
        return self

    async def __anext__(self) -> MagicMock:
        while not self._queue:
            await asyncio.sleep(0.005)
        return self._queue.pop(0)


def _patched(broker: _FakeBroker) -> Any:
    return patch("sentinel.diagnostics.aiomqtt.Client", side_effect=broker.client)


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "_EXPIRY_POLL_S", 0.01)
    monkeypatch.setattr(diagnostics, "CC2_REGISTRATION_TIMEOUT_S", 0.05)


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


async def test_discover_serial_reads_status_topic() -> None:
    broker = _FakeBroker()
    with _patched(broker):
        serial = await diagnostics.discover_serial("10.0.0.1", 1883, "secret")
    assert serial == _SERIAL
    assert broker.published == []  # listening only


async def test_discover_printer_parses_the_print_state() -> None:
    broker = _FakeBroker(print_state="paused")
    with _patched(broker):
        serial, status = await diagnostics.discover_printer("10.0.0.1", 1883, "secret")
    assert serial == _SERIAL
    assert status is not None
    assert status.label == "paused"
    assert diagnostics.is_print_active(status)


@pytest.mark.parametrize(
    ("content", "active"),
    [
        ({"print_status": {"state": "standby"}}, False),
        ({"print_status": {"state": "complete"}}, False),
        ({"print_status": {"state": "Cancelled"}}, False),
        ({"print_status": {"state": "printing"}}, True),
        ({"print_status": {"state": "paused"}}, True),
        # This firmware sends state="" while printing (see routes.py); unknown
        # states must never read as idle.
        ({"print_status": {"state": ""}}, True),
        ({"print_status": {"state": None}}, True),
        ({"print_status": {"state": "printing_start"}}, True),
        # Named states are authoritative: idle firmware reports enable true (live capture).
        ({"print_status": {"state": "standby", "enable": True}}, False),
        # Both formats present: the modern field wins over legacy Attributes.
        ({"Attributes": {"Name": "CC2"}, "print_status": {"state": "printing"}}, True),
        ({"Attributes": {"CurrentStatus": 0}}, False),
        ({"Attributes": {"CurrentStatus": 1}}, True),
        ({"Attributes": {"CurrentStatus": 7}}, True),
        ({"Attributes": {"CurrentStatus": "0"}}, False),
        ({"Attributes": {"CurrentStatus": False}}, True),
        ({"Attributes": {"CurrentStatus": 0.7}}, True),
        ({"print_status": {"state": "complete", "enable": 1}}, False),
        ({"Attributes": {"CurrentStatus": 1}, "print_status": {"state": "standby"}}, True),
        ({"Attributes": {"CurrentStatus": 0}, "print_status": {"state": "standby"}}, False),
    ],
)
def test_classify_print_state_is_fail_closed(content: dict[str, object], active: bool) -> None:
    state = diagnostics.classify_print_state(content)
    assert state is not None
    assert diagnostics.is_print_active(state) is active


def test_classify_print_state_without_a_state_is_unknown() -> None:
    assert diagnostics.classify_print_state({"print_status": {"progress": 40}}) is None
    assert diagnostics.classify_print_state({}) is None


async def test_discover_printer_without_a_state_returns_no_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics, "_SERIAL_TIMEOUT_S", 0.05)
    broker = _FakeBroker(print_state=None)
    with _patched(broker):
        serial, status = await diagnostics.discover_printer("10.0.0.1", 1883, "secret")
    assert serial == _SERIAL
    assert status is None  # a delta push is not evidence of "idle"


async def test_discover_serial_times_out_without_status_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics, "_SERIAL_TIMEOUT_S", 0.05)
    broker = _FakeBroker()
    session = _FakeSession(broker)
    session.subscribe = AsyncMock()  # type: ignore[method-assign]
    with (
        patch("sentinel.diagnostics.aiomqtt.Client", return_value=session),
        pytest.raises(Exception, match="no status push"),
    ):
        await diagnostics.discover_serial("10.0.0.1", 1883, "secret")


@pytest.mark.parametrize(
    ("broker_kwargs", "outcome"),
    [
        ({}, "ok"),
        ({"capacity": 0}, "too many clients"),
        ({"register_error": "fail"}, "fail"),
        ({"drop_register": True}, "timeout"),
        ({"malformed_register": True}, "protocol_error"),
        ({"connect_error": aiomqtt.MqttError("refused")}, "connect_error"),
        ({"connect_error": OSError("unreachable")}, "connect_error"),
    ],
)
async def test_register_once_classifies_every_answer(
    broker_kwargs: dict[str, Any], outcome: str
) -> None:
    broker = _FakeBroker(**broker_kwargs)
    reader = diagnostics.PrinterClient(_SETTINGS)
    with _patched(broker):
        result = await diagnostics.register_once(
            "10.0.0.1", 1883, "secret", _SERIAL, "0cliabc123", "req1", reader=reader, t0=0.0
        )
    assert result.outcome == outcome
    assert result.ok is (outcome == "ok")
    if outcome == "fail":
        assert "'fail'" in result.detail


async def test_register_once_mirrors_the_clients_registration_session() -> None:
    broker = _FakeBroker()
    reader = diagnostics.PrinterClient(_SETTINGS)
    with _patched(broker):
        await diagnostics.register_once(
            "10.0.0.1", 1883, "secret", _SERIAL, "0cliabc123", "req1", reader=reader, t0=0.0
        )
    assert broker.identifiers == ["0cliabc123"]
    assert broker.published == [
        (
            f"elegoo/{_SERIAL}/api_register",
            {"client_id": "0cliabc123", "request_id": "req1"},
        )
    ]
    assert broker.disconnects == 1  # one short session, closed after the response


# ---------------------------------------------------------------------------
# The experiments
# ---------------------------------------------------------------------------


async def test_fresh_ids_stop_at_first_rejection() -> None:
    broker = _FakeBroker(capacity=4)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=12)

    fresh = report["fresh"]
    # The reuse test already holds one slot; 3 more fresh ones fit, then the refusal.
    assert fresh["accepted_before_rejection"] == 3
    assert fresh["rejected"] is True
    assert fresh["rejection"]["outcome"] == "too many clients"
    assert [r["outcome"] for r in fresh["results"]] == ["ok", "ok", "ok", "too many clients"]
    assert len({r["client_id"] for r in fresh["results"]}) == 4  # fresh ids every time
    assert len({r["request_id"] for r in fresh["results"]}) == 4
    assert report["serial"] == _SERIAL


async def test_fresh_ids_report_no_rejection_up_to_max() -> None:
    broker = _FakeBroker(capacity=99)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=5)
    assert report["fresh"]["rejected"] is False
    assert report["fresh"]["accepted_before_rejection"] == 5
    assert len(report["fresh"]["results"]) == 5


@pytest.mark.parametrize(
    ("accept_reused", "verdict_start"),
    [(True, "accepted"), (False, "rejected: fail")],
)
async def test_reuse_test_registers_one_pair_on_two_sessions(
    accept_reused: bool, verdict_start: str
) -> None:
    broker = _FakeBroker(capacity=99, accept_reused_ids=accept_reused)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=1)

    reuse = report["reuse"]
    assert reuse["verdict"].startswith(verdict_start)
    assert reuse["first"]["client_id"] == reuse["second"]["client_id"]
    assert reuse["first"]["request_id"] == reuse["second"]["request_id"]
    assert reuse["first"]["outcome"] == "ok"
    assert (reuse["second"]["outcome"] == "ok") is accept_reused


async def test_reuse_is_inconclusive_when_slots_were_already_full() -> None:
    broker = _FakeBroker(capacity=0)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=3)
    assert report["reuse"]["verdict"].startswith("inconclusive")
    assert report["fresh"]["accepted_before_rejection"] == 0


async def test_expiry_wait_retries_until_a_slot_frees_up() -> None:
    broker = _FakeBroker(capacity=2, free_after_rejections=4)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(
            _SETTINGS, max_registrations=12, expiry_wait_s=5.0
        )

    expiry = report["expiry"]
    assert expiry["recovered"] is True
    # Rejection 1 ends the fresh test; retries hit rejections 2-4 (the 4th frees the
    # slots), then the fifth attempt succeeds.
    assert expiry["attempts"] == 4
    assert expiry["since_rejection_s"] >= 0
    assert expiry["since_first_registration_s"] >= expiry["since_rejection_s"]


async def test_expiry_wait_gives_up_after_the_wait() -> None:
    broker = _FakeBroker(capacity=1)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(
            _SETTINGS, max_registrations=12, expiry_wait_s=0.1
        )
    expiry = report["expiry"]
    assert expiry["recovered"] is False
    assert expiry["attempts"] >= 1
    assert expiry["since_rejection_s"] is None


async def test_expiry_not_measured_without_wait_or_without_rejection() -> None:
    broker = _FakeBroker(capacity=1)
    with _patched(broker):
        assert (await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=5))["expiry"] is None
    broker = _FakeBroker(capacity=99)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=2, expiry_wait_s=5)
    assert report["expiry"] is None


# ---------------------------------------------------------------------------
# Print-state guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["printing", "paused"])
async def test_refuses_while_a_print_is_active(state: str) -> None:
    broker = _FakeBroker(print_state=state)
    with _patched(broker), pytest.raises(diagnostics.PrinterBusyError) as exc:
        await diagnostics.run_mqtt_slots(_SETTINGS)
    assert exc.value.print_state == state
    assert state in str(exc.value)
    assert broker.published == []  # nothing was registered
    assert broker.slots == set()


async def test_refuses_when_the_print_state_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics, "_SERIAL_TIMEOUT_S", 0.05)
    snapshot = AsyncMock(return_value=None)  # GET_STATUS failed too
    monkeypatch.setattr(diagnostics, "snapshot_status", snapshot)
    broker = _FakeBroker(print_state=None)
    with _patched(broker), pytest.raises(diagnostics.PrinterBusyError) as exc:
        await diagnostics.run_mqtt_slots(_SETTINGS)
    assert exc.value.print_state == "unknown"
    snapshot.assert_awaited_once()
    assert broker.published == []


@pytest.mark.parametrize("state", ["printing", "paused"])
async def test_force_runs_during_a_print(state: str) -> None:
    broker = _FakeBroker(capacity=3, print_state=state)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=6, force=True)
    assert report["forced"] is True
    assert report["print_state"] == state
    assert report["fresh"]["rejected"] is True
    assert broker.published


async def test_idle_run_reports_the_print_state() -> None:
    broker = _FakeBroker(capacity=99)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=2)
    assert report["print_state"] == "standby"
    assert report["forced"] is False


async def test_expiry_wait_stops_when_a_print_starts() -> None:
    broker = _FakeBroker(capacity=2, start_print_after_rejections=3)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(
            _SETTINGS, max_registrations=12, expiry_wait_s=5.0
        )

    expiry = report["expiry"]
    assert expiry["recovered"] is False
    assert "a print started" in expiry["stopped_reason"]
    assert "printing" in expiry["stopped_reason"]
    # Rejection 1 ends the fresh test, retry 1 is rejection 2, retry 2 is rejection 3
    # (which starts the print); the re-check before retry 3 stops the run.
    assert expiry["attempts"] == 2
    assert broker.rejections == 3  # no registration was attempted after the print began
    summary = diagnostics.format_summary(report)
    assert "Expiry: stopped early" in summary
    assert "a print started" in summary


async def test_force_skips_the_expiry_recheck() -> None:
    broker = _FakeBroker(capacity=2, start_print_after_rejections=3, free_after_rejections=5)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(
            _SETTINGS, max_registrations=12, expiry_wait_s=5.0, force=True
        )
    assert report["expiry"]["recovered"] is True
    assert report["expiry"]["stopped_reason"] is None


async def test_expiry_wait_stops_when_the_state_cannot_be_rechecked() -> None:
    broker = _FakeBroker(capacity=2)

    async def dead(self: Any) -> Any:
        raise aiomqtt.MqttError("status subscription lost")

    with _patched(broker), patch.object(diagnostics._StateWatch, "latest", dead):
        report = await diagnostics.run_mqtt_slots(
            _SETTINGS, max_registrations=12, expiry_wait_s=5.0
        )
    assert report["expiry"]["attempts"] == 0
    assert "could not re-check" in report["expiry"]["stopped_reason"]


async def test_only_registrations_are_ever_published() -> None:
    """The tool must never send pause/resume/stop or any command to api_request."""
    broker = _FakeBroker(capacity=3, free_after_rejections=3)
    with _patched(broker):
        await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=12, expiry_wait_s=5)

    assert broker.published
    assert all(topic.endswith("/api_register") for topic, _ in broker.published)
    assert all(set(payload) == {"client_id", "request_id"} for _, payload in broker.published)
    assert all("method" not in payload for _, payload in broker.published)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_prints_warning_summary_and_json_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRINTER_IP", "10.0.0.1")
    monkeypatch.setenv("PRINTER_ACCESS_CODE", "secret")
    broker = _FakeBroker(capacity=3)
    with _patched(broker):
        code = diagnostics.main(["mqtt-slots", "--max", "6"])

    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert code == 0
    assert lines[0].startswith("WARNING")
    assert "Run it while the printer is idle" in out
    assert "Fresh ids: 2 accepted, then 'too many clients'" in out
    report = json.loads(lines[-1])
    assert report["fresh"]["accepted_before_rejection"] == 2
    assert report["serial"] == _SERIAL


def test_cli_reports_failure_as_json_and_exit_code_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRINTER_IP", "10.0.0.1")
    monkeypatch.setenv("PRINTER_ACCESS_CODE", "secret")
    broker = _FakeBroker(connect_error=OSError("unreachable"))
    with _patched(broker):
        code = diagnostics.main(["mqtt-slots"])

    out = capsys.readouterr().out.strip().splitlines()
    assert code == 1
    assert out[-2].startswith("FAIL: OSError")
    assert "error" in json.loads(out[-1])


def test_cli_refuses_during_a_print_with_exit_code_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRINTER_IP", "10.0.0.1")
    monkeypatch.setenv("PRINTER_ACCESS_CODE", "secret")
    broker = _FakeBroker(print_state="printing")
    with _patched(broker):
        code = diagnostics.main(["mqtt-slots"])

    out = capsys.readouterr().out.strip().splitlines()
    assert code == 2
    assert out[-2].startswith("REFUSED:")
    assert "--force" in out[-2]
    payload = json.loads(out[-1])
    assert payload["refused"] is True
    assert payload["print_state"] == "printing"
    assert broker.published == []


def test_cli_force_runs_during_a_print(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRINTER_IP", "10.0.0.1")
    monkeypatch.setenv("PRINTER_ACCESS_CODE", "secret")
    broker = _FakeBroker(capacity=3, print_state="printing")
    with _patched(broker):
        code = diagnostics.main(["mqtt-slots", "--force", "--max", "6"])

    out = capsys.readouterr().out
    assert code == 0
    assert "print state printing  (forced)" in out


def test_cli_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit) as exc:
        diagnostics.main([])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Printer address: the dashboard-saved address wins over PRINTER_IP, as in the service
# ---------------------------------------------------------------------------


def _db_with_printer_ip(path: Any, value: str | None) -> str:
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runtime_settings (key TEXT PRIMARY KEY, value TEXT)")
    if value is not None:
        conn.execute("INSERT INTO runtime_settings VALUES ('printer_ip', ?)", (value,))
    conn.commit()
    conn.close()
    return str(path)


def test_stored_printer_ip_reads_the_saved_address(tmp_path: Any) -> None:
    db = _db_with_printer_ip(tmp_path / "s.db", "192.168.40.20")
    assert diagnostics.stored_printer_ip(db) == "192.168.40.20"


def test_stored_printer_ip_is_none_without_a_saved_address(tmp_path: Any) -> None:
    assert diagnostics.stored_printer_ip(_db_with_printer_ip(tmp_path / "s.db", None)) is None
    assert diagnostics.stored_printer_ip(str(tmp_path / "missing.db")) is None
    assert not (tmp_path / "missing.db").exists(), "must not create the database"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("192.168.40.20", "192.168.40.20"), (None, "10.0.0.1"), ("not a host!", "10.0.0.1")],
)
async def test_mqtt_slots_dials_the_address_the_service_uses(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, stored: str | None, expected: str
) -> None:
    """Production saved a new address from the dashboard; the diagnostic kept dialling the
    stale PRINTER_IP and failed with 'No route to host' while the service was connected.
    A saved address that fails validation is ignored, as the service ignores it."""
    db = _db_with_printer_ip(tmp_path / "s.db", stored)
    settings = Settings(printer_ip="10.0.0.1", printer_access_code="secret", db_path=db)
    seen: dict[str, str] = {}

    async def fake_run(s: Settings, **_: Any) -> dict[str, Any]:
        seen["ip"] = s.printer_ip
        return {}

    monkeypatch.setattr("sentinel.config.get_settings", lambda: settings)
    monkeypatch.setattr(diagnostics, "run_mqtt_slots", fake_run)
    monkeypatch.setattr(diagnostics, "format_summary", lambda _r: "")
    args = diagnostics._build_parser().parse_args(["mqtt-slots"])
    assert await diagnostics._mqtt_slots(args) == 0
    assert seen["ip"] == expected


# ---------------------------------------------------------------------------
# Quiet idle printer: deltas only (live probe 2026-10-21)
# ---------------------------------------------------------------------------

_IDLE_SNAPSHOT_RESULT: dict[str, Any] = {
    "error_code": 0,
    "machine_status": {"status": 1},
    "print_status": {"state": "", "enable": True, "filename": ""},
}


@pytest.mark.parametrize(
    ("content", "active"),
    [
        (_IDLE_SNAPSHOT_RESULT, False),
        ({"print_status": {"state": ""}, "machine_status": {"status": 2}}, True),
        ({"print_status": {"state": ""}}, True),
        ({"print_status": {"state": "", "enable": True}, "machine_status": {}}, True),
    ],
)
def test_empty_state_is_idle_only_with_the_idle_status_code(
    content: dict[str, Any], active: bool
) -> None:
    state = diagnostics.classify_print_state(content)
    assert state is not None
    assert diagnostics.is_print_active(state) is active


async def test_quiet_idle_printer_uses_http_serial_and_get_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production: no push within 15 s, so no serial and no state. The run must learn
    the serial over HTTP and the state from GET_STATUS, then proceed."""

    async def no_push(*_: Any) -> Any:
        raise diagnostics.PrinterTimeoutError("no status push within 15s")

    http = AsyncMock(return_value=_SERIAL)
    snapshot = AsyncMock(return_value=_IDLE_SNAPSHOT_RESULT)
    monkeypatch.setattr(diagnostics, "discover_printer", no_push)
    monkeypatch.setattr(diagnostics, "http_serial", http)
    monkeypatch.setattr(diagnostics, "snapshot_status", snapshot)
    broker = _FakeBroker(capacity=2, print_state=None)
    with _patched(broker):
        report = await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=5)
    assert report["serial"] == _SERIAL
    assert report["print_state_source"] == "GET_STATUS"
    assert report["registrations_before_tests"] == 1
    assert report["fresh"]["accepted_before_rejection"] == 1  # the reuse pair holds one slot
    snapshot.assert_awaited_once()


async def test_refuses_when_neither_push_nor_http_gives_a_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_push(*_: Any) -> Any:
        raise diagnostics.PrinterTimeoutError("no status push within 15s")

    monkeypatch.setattr(diagnostics, "discover_printer", no_push)
    monkeypatch.setattr(diagnostics, "http_serial", AsyncMock(return_value=None))
    broker = _FakeBroker()
    with _patched(broker), pytest.raises(diagnostics.PrinterTimeoutError, match="system/info"):
        await diagnostics.run_mqtt_slots(_SETTINGS)
    assert broker.published == []


async def test_snapshot_status_reads_get_status() -> None:
    reader = MagicMock()
    reader._send_command = AsyncMock(
        return_value={"id": 1, "method": 1002, "result": _IDLE_SNAPSHOT_RESULT}
    )
    content = await diagnostics.snapshot_status(reader, _SERIAL)
    reader._send_command.assert_awaited_once_with(1002)
    assert reader._serial_number == _SERIAL
    assert content == _IDLE_SNAPSHOT_RESULT


async def test_snapshot_status_is_none_on_failure() -> None:
    reader = MagicMock()
    reader._send_command = AsyncMock(side_effect=diagnostics.PrinterTimeoutError("no ack"))
    assert await diagnostics.snapshot_status(reader, _SERIAL) is None


async def test_state_watch_sees_a_machine_status_change_from_the_idle_seed() -> None:
    """A print start that pushes only machine_status (no print_status.state) must read
    as busy once the watch starts from the idle snapshot."""
    broker = _FakeBroker(print_state=None)
    with _patched(broker):
        async with diagnostics._StateWatch("10.0.0.1", 1883, "secret") as watch:
            watch.seed(_IDLE_SNAPSHOT_RESULT)
            first = await watch.latest()
            assert first is not None and not diagnostics.is_print_active(first)
            broker.status_sessions[0]._queue.append(
                _message(
                    f"elegoo/{_SERIAL}/api_status",
                    {"method": 6000, "result": {"machine_status": {"status": 2}}},
                )
            )
            later = await watch.latest()
    assert later is not None and diagnostics.is_print_active(later)


async def test_state_watch_returns_under_continuous_pushes() -> None:
    broker = _FakeBroker()
    with _patched(broker):
        async with diagnostics._StateWatch("10.0.0.1", 1883, "secret") as watch:
            session = broker.status_sessions[0]

            async def flood() -> None:
                while True:
                    session.push_state()
                    await asyncio.sleep(0.001)

            pusher = asyncio.create_task(flood())
            try:
                state = await asyncio.wait_for(watch.latest(), 1.0)
            finally:
                pusher.cancel()
                await asyncio.gather(pusher, return_exceptions=True)
    assert state is not None and state.label == "standby"


async def test_state_watch_reports_a_closed_subscription() -> None:
    broker = _FakeBroker()
    with _patched(broker):
        async with diagnostics._StateWatch("10.0.0.1", 1883, "secret") as watch:
            watch._error = aiomqtt.MqttError("lost")
            with pytest.raises(aiomqtt.MqttError):
                await watch.latest()


async def test_watch_opens_before_the_state_check(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    real_enter = diagnostics._StateWatch.__aenter__

    async def enter(self: Any) -> Any:
        order.append("watch")
        return await real_enter(self)

    real_discover = diagnostics.discover_printer

    async def discover(*a: Any) -> Any:
        order.append("check")
        return await real_discover(*a)

    monkeypatch.setattr(diagnostics._StateWatch, "__aenter__", enter)
    monkeypatch.setattr(diagnostics, "discover_printer", discover)
    broker = _FakeBroker(capacity=99)
    with _patched(broker):
        await diagnostics.run_mqtt_slots(_SETTINGS, max_registrations=1)
    assert order[:2] == ["watch", "check"]
