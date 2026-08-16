"""
Local Runtime Helpers Test Suite

Executes unit tests for local runtime helper functions,
ensuring correct behaviour of dependency checks and URL waiting mechanisms.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from src import local_smoke_test, run_with_dependency_checks


def _cm(value):
    """Creates a context manager that returns the given value."""
    manager = MagicMock()
    manager.__enter__.return_value = value
    manager.__exit__.return_value = False
    return manager


def test_extract_host_port_from_connection_string_variants():
    """Proves that the host and port are correctly extracted from various connection string formats."""
    host, port = run_with_dependency_checks._extract_host_port_from_connection_string(
        "Endpoint=sb://servicebus-emulator:5672/;SharedAccessKeyName=x;SharedAccessKey=y"
    )
    assert host == "servicebus-emulator"
    assert port == 5672

    host, port = run_with_dependency_checks._extract_host_port_from_connection_string(
        "Endpoint=sb://localhost/;SharedAccessKeyName=x;SharedAccessKey=y"
    )
    assert host == "localhost"
    assert port == 5672

    host, port = run_with_dependency_checks._extract_host_port_from_connection_string(
        "Endpoint=sb://localhost:notaport/;SharedAccessKeyName=x;SharedAccessKey=y"
    )
    assert host == "localhost"
    assert port == 5672


def test_should_wait_for_emulator_detection():
    """Proves that the emulator detection logic correctly identifies connection strings that require waiting."""
    assert (
        run_with_dependency_checks._should_wait_for_emulator(
            "Endpoint=sb://example/;UseDevelopmentEmulator=true"
        )
        is True
    )
    assert (
        run_with_dependency_checks._should_wait_for_emulator(
            "Endpoint=sb://servicebus-emulator/"
        )
        is True
    )
    assert (
        run_with_dependency_checks._should_wait_for_emulator(
            "Endpoint=sb://example-dev.servicebus.windows.net/"
        )
        is False
    )


def test_run_dependency_checks_disabled(monkeypatch):
    """Proves that dependency checks are skipped when disabled via environment variable."""
    monkeypatch.setenv("ENABLE_DEPENDENCY_WAIT", "false")
    wait_tcp = MagicMock()
    wait_http = MagicMock()
    monkeypatch.setattr(run_with_dependency_checks, "_wait_for_tcp", wait_tcp)
    monkeypatch.setattr(run_with_dependency_checks, "_wait_for_http", wait_http)

    run_with_dependency_checks._run_dependency_checks()

    wait_tcp.assert_not_called()
    wait_http.assert_not_called()


def test_run_dependency_checks_success_path(monkeypatch):
    """Proves that dependency checks are executed and succeed when enabled."""
    monkeypatch.setenv(
        "SERVICE_BUS_CONNECTION_STRING",
        "Endpoint=sb://servicebus-emulator:5678/;UseDevelopmentEmulator=true",
    )
    monkeypatch.setenv("EMULATOR_HTTP_PORT", "5301")
    monkeypatch.setenv("DEPENDENCY_WAIT_TIMEOUT_SECONDS", "9")

    wait_tcp = MagicMock(return_value=True)
    wait_http = MagicMock(return_value=True)
    monkeypatch.setattr(run_with_dependency_checks, "_wait_for_tcp", wait_tcp)
    monkeypatch.setattr(run_with_dependency_checks, "_wait_for_http", wait_http)

    run_with_dependency_checks._run_dependency_checks()

    wait_tcp.assert_called_once_with("servicebus-emulator", 5678, 9)
    wait_http.assert_called_once_with("http://servicebus-emulator:5301/health", 9)


def test_run_dependency_checks_tcp_timeout_skips_http(monkeypatch):
    """Proves that if the TCP wait fails, the HTTP wait is skipped."""
    monkeypatch.setenv(
        "SERVICE_BUS_CONNECTION_STRING",
        "Endpoint=sb://servicebus-emulator/;UseDevelopmentEmulator=true",
    )

    wait_tcp = MagicMock(return_value=False)
    wait_http = MagicMock(return_value=True)
    monkeypatch.setattr(run_with_dependency_checks, "_wait_for_tcp", wait_tcp)
    monkeypatch.setattr(run_with_dependency_checks, "_wait_for_http", wait_http)

    run_with_dependency_checks._run_dependency_checks()

    wait_tcp.assert_called_once()
    wait_http.assert_not_called()


def test_run_with_dependency_checks_main_calls_agent_main(monkeypatch):
    """Proves that the main function runs dependency checks before calling the agent main."""
    run_checks = MagicMock()
    agent_main = MagicMock()
    monkeypatch.setattr(
        run_with_dependency_checks, "_run_dependency_checks", run_checks
    )
    monkeypatch.setattr(run_with_dependency_checks.run_agent, "main", agent_main)

    run_with_dependency_checks.main()

    run_checks.assert_called_once()
    agent_main.assert_called_once()


def test_wait_for_tcp_success(monkeypatch):
    """Proves that the TCP wait function successfully establishes a connection."""
    created = MagicMock(return_value=_cm(object()))
    monkeypatch.setattr(run_with_dependency_checks.socket, "create_connection", created)
    monkeypatch.setattr(run_with_dependency_checks.time, "time", lambda: 0)

    assert run_with_dependency_checks._wait_for_tcp("host", 5672, 1) is True
    created.assert_called_once_with(("host", 5672), timeout=2)


def test_wait_for_http_success(monkeypatch):
    """Proves that the HTTP wait function successfully receives a response."""
    response = SimpleNamespace(status=200)
    urlopen = MagicMock(return_value=_cm(response))
    monkeypatch.setattr(run_with_dependency_checks.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(run_with_dependency_checks.time, "time", lambda: 0)

    assert run_with_dependency_checks._wait_for_http("http://health", 1) is True
    urlopen.assert_called_once_with("http://health", timeout=2)


def test_wait_for_url_retries_then_succeeds(monkeypatch):
    """Proves that the URL wait function retries on failure and eventually succeeds."""
    calls = {"n": 0}

    def fake_get_json(_url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("not ready")
        return {"ok": True}

    now = {"t": 0.0}

    def fake_time():
        now["t"] += 0.1
        return now["t"]

    monkeypatch.setattr(local_smoke_test, "_get_json", fake_get_json)
    monkeypatch.setattr(local_smoke_test.time, "time", fake_time)
    monkeypatch.setattr(local_smoke_test.time, "sleep", lambda _s: None)

    assert local_smoke_test._wait_for_url("http://health", 2) is True


def test_run_dependency_checks_unparseable_endpoint(monkeypatch):
    """Proves that dependency checks are skipped when the endpoint is unparseable."""
    monkeypatch.setenv(
        "SERVICE_BUS_CONNECTION_STRING",
        "Endpoint=sb:///;UseDevelopmentEmulator=true",
    )
    wait_tcp = MagicMock(return_value=True)
    wait_http = MagicMock(return_value=True)
    monkeypatch.setattr(run_with_dependency_checks, "_wait_for_tcp", wait_tcp)
    monkeypatch.setattr(run_with_dependency_checks, "_wait_for_http", wait_http)

    run_with_dependency_checks._run_dependency_checks()

    wait_tcp.assert_not_called()
    wait_http.assert_not_called()


def test_extract_property_handles_bytes_keys_and_values():
    """Proves that the property extraction function correctly handles byte keys and values."""
    message = SimpleNamespace(
        application_properties={b"smoke_test_id": b"abc-123", "other": "value"}
    )
    assert local_smoke_test._extract_property(message, "smoke_test_id") == "abc-123"
    assert local_smoke_test._extract_property(message, "missing") is None


def test_smoke_main_missing_connection_string(monkeypatch):
    """Proves that the main function returns an error when the connection string is missing."""
    monkeypatch.delenv("SERVICE_BUS_CONNECTION_STRING", raising=False)
    assert local_smoke_test.main() == 1


def test_smoke_main_agent_health_not_ready(monkeypatch):
    """Proves that the main function returns an error when the agent health check fails."""
    monkeypatch.setenv("SERVICE_BUS_CONNECTION_STRING", "Endpoint=sb://mock/")
    monkeypatch.setattr(
        local_smoke_test, "_wait_for_url", MagicMock(return_value=False)
    )

    assert local_smoke_test.main() == 1


def test_smoke_main_emulator_health_not_ready(monkeypatch):
    """Proves that the main function returns an error when the emulator health check fails."""
    monkeypatch.setenv("SERVICE_BUS_CONNECTION_STRING", "Endpoint=sb://mock/")
    monkeypatch.setattr(
        local_smoke_test,
        "_wait_for_url",
        MagicMock(side_effect=[True, False]),
    )

    assert local_smoke_test.main() == 1


def test_smoke_main_message_not_found_returns_error(monkeypatch):
    """Proves that the main function returns an error when the smoke test message is not found."""
    monkeypatch.setenv("SERVICE_BUS_CONNECTION_STRING", "Endpoint=sb://mock/")
    monkeypatch.setenv("SMOKE_TEST_PROCESSING_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(local_smoke_test, "_wait_for_url", MagicMock(return_value=True))
    monkeypatch.setattr(
        local_smoke_test,
        "_get_json",
        MagicMock(side_effect=[{"counters": {"messages_processed_total": 0}}]),
    )
    monkeypatch.setattr(local_smoke_test.uuid, "uuid4", lambda: "smoke-id")

    sender = MagicMock()
    receiver = MagicMock()
    receiver.receive_messages.return_value = [
        SimpleNamespace(application_properties={b"smoke_test_id": b"different-id"})
    ]

    sb_client = MagicMock()
    sb_client.get_queue_sender.return_value = _cm(sender)
    sb_client.get_queue_receiver.return_value = _cm(receiver)

    service_bus_client = MagicMock()
    service_bus_client.from_connection_string.return_value = _cm(sb_client)
    monkeypatch.setattr(local_smoke_test, "ServiceBusClient", service_bus_client)

    assert local_smoke_test.main() == 1
    receiver.abandon_message.assert_called_once()
    receiver.dead_letter_message.assert_not_called()


def test_smoke_main_success_path(monkeypatch):
    """Proves that the main function succeeds when the smoke test message is processed."""
    monkeypatch.setenv("SERVICE_BUS_CONNECTION_STRING", "Endpoint=sb://mock/")
    monkeypatch.setenv("SMOKE_TEST_PROCESSING_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(local_smoke_test, "_wait_for_url", MagicMock(return_value=True))
    monkeypatch.setattr(
        local_smoke_test,
        "_get_json",
        MagicMock(
            side_effect=[
                {"counters": {"messages_processed_total": 0}},
                {"counters": {"messages_processed_total": 1}},
            ]
        ),
    )
    monkeypatch.setattr(local_smoke_test.uuid, "uuid4", lambda: "smoke-id")

    sender = MagicMock()
    receiver = MagicMock()
    receiver.receive_messages.return_value = [
        SimpleNamespace(application_properties={b"smoke_test_id": b"smoke-id"})
    ]

    sb_client = MagicMock()
    sb_client.get_queue_sender.return_value = _cm(sender)
    sb_client.get_queue_receiver.return_value = _cm(receiver)

    service_bus_client = MagicMock()
    service_bus_client.from_connection_string.return_value = _cm(sb_client)
    monkeypatch.setattr(local_smoke_test, "ServiceBusClient", service_bus_client)
    monkeypatch.setattr(
        local_smoke_test,
        "ServiceBusMessage",
        lambda body, application_properties, correlation_id: {
            "body": json.loads(body),
            "application_properties": application_properties,
            "correlation_id": correlation_id,
        },
    )

    assert local_smoke_test.main() == 0
    sender.send_messages.assert_called_once()
    receiver.dead_letter_message.assert_called_once()


def test_smoke_main_processing_timeout_returns_error(monkeypatch):
    """Proves that the main function returns an error when the smoke test processing times out."""
    monkeypatch.setenv("SERVICE_BUS_CONNECTION_STRING", "Endpoint=sb://mock/")
    monkeypatch.setenv("SMOKE_TEST_PROCESSING_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(local_smoke_test, "_wait_for_url", MagicMock(return_value=True))
    monkeypatch.setattr(
        local_smoke_test,
        "_get_json",
        MagicMock(
            side_effect=[
                {"counters": {"messages_processed_total": 0}},
                {"counters": {"messages_processed_total": 0, "failures_total": 0}},
            ]
        ),
    )
    monkeypatch.setattr(local_smoke_test.uuid, "uuid4", lambda: "smoke-id")

    sender = MagicMock()
    receiver = MagicMock()
    receiver.receive_messages.return_value = [
        SimpleNamespace(application_properties={b"smoke_test_id": b"smoke-id"})
    ]

    sb_client = MagicMock()
    sb_client.get_queue_sender.return_value = _cm(sender)
    sb_client.get_queue_receiver.return_value = _cm(receiver)

    service_bus_client = MagicMock()
    service_bus_client.from_connection_string.return_value = _cm(sb_client)
    monkeypatch.setattr(local_smoke_test, "ServiceBusClient", service_bus_client)
    monkeypatch.setattr(
        local_smoke_test,
        "ServiceBusMessage",
        lambda body, application_properties, correlation_id: {
            "body": json.loads(body),
            "application_properties": application_properties,
            "correlation_id": correlation_id,
        },
    )

    assert local_smoke_test.main() == 1
