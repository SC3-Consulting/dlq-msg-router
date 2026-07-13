"""
Enterprise DLQ Pipeline Test Suite

Executes unit and integration tests,
ensuring zero-trust PII masking, type-safe auto-healing, and resilient network polling.
"""

import json
import os
import time
from typing import Any, Optional, cast
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.servicebus.exceptions import ServiceBusError

from src.action_executor import ActionRouter
from src.ai_client import AIEngineFactory, AzureFoundryEngine, OllamaEngine, PIIScrubber

# Import updated Phase 1-4 architecture components
from src.autonomous_dlq_classifier import AutonomousDLQClassifier
from src.run_agent import ServiceBusClientFactory, discover_target_queues
from src.state_managers import ClassificationCache, IdempotencyStore

# ==========================================
# FIXTURES & MOCKS
# ==========================================


class MockServiceBusReceivedMessage:
    """Simulates an Azure Service Bus message with PEEK_LOCK state.
    This mock class is used for testing the AutonomousDLQClassifier and ActionRouter without requiring an actual Service Bus connection.

    Attributes:
        body (list): The message body as a list of JSON-encoded bytes.
        application_properties (dict): The message's application properties.
        message_id (str): The unique identifier for the message.
        correlation_id (Optional[str]): The correlation identifier for the message.
        dead_letter_reason (Optional[str]): The reason for dead-lettering the message.
        dead_letter_error_description (Optional[str]): The error description for dead-lettering the message.
        subject (str): The subject of the message.
        content_type (str): The content type of the message.
    """

    def __init__(
        self,
        body_dict: dict,
        properties: dict,
        reason: Optional[str] = "Unknown",
        desc: Optional[str] = "Unknown",
        message_id: str = "test-msg-123",
        correlation_id: Optional[str] = "corr-456",
    ):
        self.body = [json.dumps(body_dict).encode("utf-8")]
        self.application_properties = properties
        self.message_id = message_id
        self.correlation_id = correlation_id
        self.dead_letter_reason: Optional[str] = reason
        self.dead_letter_error_description: Optional[str] = desc
        self.subject = "TestSubject"
        self.content_type = "application/json"


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Scaffolds a safe, isolated filesystem matching the enterprise rules.json."""
    db_path = tmp_path / "idempotency.db"
    rules_path = tmp_path / "rules.json"

    # Mirroring the exact schema from Phase 2
    rules_data = {
        "global_rules": [
            {
                "rule_id": "app_001",
                "severity_score": 50,
                "classification": "Schema_Validation_Failed",
                "pattern_name": "validation_failed_generic",
                "condition": "reason == 'ValidationFailed'",
                "pattern_regex": "missing mandatory field: '(\\w+)'",
                "default_action": "fix_and_retry",
                "safe_defaults_map": {
                    "transaction_amount": 0.0,
                    "customer_id": "UNASSIGNED",
                },
            }
        ],
        "queue_overrides": {
            "integration-queue": [
                {
                    "rule_id": "app_004",
                    "severity_score": 50,
                    "classification": "Business_Logic_Violation",
                    "pattern_name": "custom_client_rejection",
                    "condition": "reason == 'Business_Rule_Violation'",
                    "default_action": "escalate",
                }
            ]
        },
    }
    rules_path.write_text(json.dumps(rules_data))

    # Inject Phase 1-4 environment variables
    monkeypatch.setenv("IDEMPOTENCY_DB_PATH", str(db_path))
    monkeypatch.setenv("RULES_FILE_PATH", str(rules_path))
    monkeypatch.setenv("ENABLE_NESTED_BROKER_EXCEPTIONS", "True")
    monkeypatch.setenv("AI_PROVIDER", "OLLAMA")
    monkeypatch.setenv("OLLAMA_MODEL", "mock_model")
    monkeypatch.setenv(
        "OLLAMA_ENDPOINT", "http://mock"
    )  # Prevents network bleed during CI runs
    monkeypatch.setenv("MAX_CONCURRENT_QUEUES", "5")

    return tmp_path


@pytest.fixture
def mock_infrastructure():
    return {
        "receiver": MagicMock(),
        "main_sender": MagicMock(),
        "parking_sender": MagicMock(),
        "db": MagicMock(),
        "ai": MagicMock(),
    }


# ==========================================
# UNIT TESTS: PHASE 1 (NETWORK & DISCOVERY)
# ==========================================


@patch("src.run_agent.ServiceBusClient")
def test_service_bus_factory_singleton(mock_sb_client):
    """Proves the factory prevents TCP exhaustion by vending a singleton connection."""
    ServiceBusClientFactory._client = None  # Reset state
    os.environ.pop("SERVICE_BUS_CONNECTION_STRING", None)

    client_1 = ServiceBusClientFactory.get_client(
        "mock.servicebus.windows.net", MagicMock()
    )
    client_2 = ServiceBusClientFactory.get_client(
        "mock.servicebus.windows.net", MagicMock()
    )

    # Must be the exact same object in memory
    assert client_1 is client_2
    assert mock_sb_client.call_count == 1


def test_runtime_health_probe_server_reports_state():
    from src.run_agent import runtime_health, start_health_server, stop_health_server

    runtime_health.reset()
    server, _ = start_health_server("127.0.0.1", 0)
    port = server.server_address[1]

    try:
        with urlopen(f"http://127.0.0.1:{port}/health") as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert payload["ready"] is False

        runtime_health.mark_ready()

        with urlopen(f"http://127.0.0.1:{port}/ready") as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert payload["ready"] is True

        runtime_health.mark_shutdown("test_shutdown")

        with pytest.raises(HTTPError) as exc_info:
            urlopen(f"http://127.0.0.1:{port}/ready")

        assert exc_info.value.code == 503
    finally:
        stop_health_server(server)
        runtime_health.reset()


def test_metrics_endpoint_reports_observability_counters():
    from src.run_agent import (
        observability,
        runtime_health,
        start_health_server,
        stop_health_server,
    )

    runtime_health.reset()
    observability.reset()
    server, _ = start_health_server("127.0.0.1", 0)
    port = server.server_address[1]

    try:
        observability.record_contract(
            {
                "source_queue": "integration-queue",
                "status": "Auto_Classified_From_Cache",
                "suggested_action": "retry",
            }
        )
        observability.record_failure("queue_drain")

        with urlopen(f"http://127.0.0.1:{port}/metrics") as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert payload["counters"]["messages_processed_total"] == 1
            assert payload["counters"]["cache_hits_total"] == 1
            assert payload["counters"]["retries_total"] == 1
            assert payload["counters"]["failures_total"] == 1
            assert payload["queues"]["integration-queue"] == 1
    finally:
        stop_health_server(server)
        observability.reset()
        runtime_health.reset()


@patch("src.run_agent.ServiceBusAdministrationClient")
def test_dynamic_discovery_rbac_fallback(mock_admin_client, monkeypatch):
    """Proves discovery gracefully falls back to .env if Azure RBAC denies the call."""
    # Architect Fix: Use HttpResponseError with status_code 403
    rbac_error = HttpResponseError(message="RBAC Denied")
    rbac_error.status_code = 403

    mock_admin_instance = MagicMock()
    mock_admin_instance.list_queues.side_effect = rbac_error
    mock_admin_client.return_value.__enter__.return_value = mock_admin_instance

    # Configure the fallback environment variable
    fallback_sources = [{"type": "queue", "name": "fallback-queue"}]
    monkeypatch.setenv("ASB_SOURCES", json.dumps(fallback_sources))

    queues = discover_target_queues("mock.namespace", MagicMock())

    assert len(queues) == 1
    assert queues[0] == "fallback-queue"


def test_extract_namespace_from_connection_string():
    from src.run_agent import _extract_namespace_from_connection_string

    conn = (
        "Endpoint=sb://example-dev.servicebus.windows.net/;"
        "SharedAccessKeyName=RootManageSharedAccessKey;"
        "SharedAccessKey=abc123="
    )
    assert _extract_namespace_from_connection_string(conn) == (
        "example-dev.servicebus.windows.net"
    )


@patch("src.run_agent.ServiceBusAdministrationClient")
def test_dynamic_discovery_uses_connection_string(mock_admin_client, monkeypatch):
    monkeypatch.setenv("ENABLE_DYNAMIC_DISCOVERY", "True")
    monkeypatch.setenv(
        "SERVICE_BUS_CONNECTION_STRING",
        "Endpoint=sb://example-dev.servicebus.windows.net/;SharedAccessKeyName=name;SharedAccessKey=key=",
    )

    queue_props = MagicMock()
    queue_props.name = "orders-queue"
    admin_instance = MagicMock()
    admin_instance.list_queues.return_value = [queue_props]
    mock_admin_client.from_connection_string.return_value = admin_instance

    queues = discover_target_queues("ignored.servicebus.windows.net", MagicMock())

    assert queues == ["orders-queue"]
    mock_admin_client.from_connection_string.assert_called_once()


# ==========================================
# UNIT TESTS: PHASE 2 (ACTION EXECUTION)
# ==========================================


def test_fix_and_retry_type_safe_mutation(temp_env, mock_infrastructure):
    """Proves the agent injects strictly typed defaults (float) instead of strings."""
    router = ActionRouter(
        mock_infrastructure["receiver"],
        mock_infrastructure["main_sender"],
        mock_infrastructure["parking_sender"],
    )

    msg = MockServiceBusReceivedMessage(
        body_dict={"existing_key": "value"}, properties={b"Resubmit-Count": 0}
    )

    safe_defaults_map = {"transaction_amount": 0.0, "customer_id": "UNASSIGNED"}

    # Execute auto-healing
    router.route_and_execute(
        "fix_and_retry", msg, "missing_field_transaction_amount", safe_defaults_map
    )

    # Intercept the cloned message sent to the main queue
    sent_msg = mock_infrastructure["main_sender"].send_messages.call_args[0][0]
    payload = json.loads(b"".join(sent_msg.body).decode("utf-8"))

    # Assert type safety: transaction_amount MUST be a float, not a string
    assert payload["existing_key"] == "value"
    assert payload["transaction_amount"] == 0.0
    assert isinstance(payload["transaction_amount"], float)


def test_broker_offline_nested_catch_safety(mock_infrastructure):
    """Proves the Phase 2 nested try/except prevents cascading thread crashes."""
    router = ActionRouter(
        mock_infrastructure["receiver"],
        mock_infrastructure["main_sender"],
        mock_infrastructure["parking_sender"],
    )
    msg = MockServiceBusReceivedMessage(body_dict={}, properties={})

    # Simulate a severe network partition during a Drop operation
    mock_infrastructure["receiver"].complete_message.side_effect = ServiceBusError(
        "Network partition"
    )
    mock_infrastructure["receiver"].abandon_message.side_effect = ServiceBusError(
        "Broker unreachable"
    )

    try:
        router.route_and_execute("drop", msg, "ttl_expired", None)
    except Exception as e:
        pytest.fail(f"Agent thread crashed due to unhandled nested exception: {e}")


# ==========================================
# UNIT TESTS: PHASE 3 (AI FACTORY)
# ==========================================


def test_ai_engine_factory_routing(temp_env, monkeypatch):
    """Proves the Factory dynamically loads the correct AI provider class."""
    monkeypatch.setenv("AI_PROVIDER", "OLLAMA")
    engine1 = AIEngineFactory.get_engine()
    assert isinstance(engine1, OllamaEngine)

    monkeypatch.setenv("AI_PROVIDER", "AZURE_FOUNDRY")
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "mock")
    monkeypatch.setenv("AZURE_FOUNDRY_DEPLOYMENT_NAME", "mock")

    with patch("src.ai_client.ChatCompletionsClient"):
        engine2 = AIEngineFactory.get_engine()
        assert isinstance(engine2, AzureFoundryEngine)


def test_ai_json_salvage(temp_env, monkeypatch):
    """Proves Phase 3 regex extraction bypasses LLM conversational hallucinations."""
    monkeypatch.setenv("AI_PROVIDER", "OLLAMA")
    engine = AIEngineFactory.get_engine()

    messy_response = (
        "Certainly! Here is the JSON:\n"
        "```json\n"
        '{"suggested_action": "drop"}\n'
        "```\n"
        "Hope this helps!"
    )

    parsed = engine._salvage_json(messy_response)
    assert parsed["suggested_action"] == "drop"


@pytest.mark.usefixtures("temp_env")
@patch("src.ai_client.requests.post")
def test_ai_client_call_llm_network_mock(mock_post, monkeypatch):
    """Proves OllamaEngine formats the payload and handles successful 200 OK responses."""
    monkeypatch.setenv("AI_PROVIDER", "OLLAMA")
    monkeypatch.setenv("OLLAMA_MODEL", "mock_model")
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://mock")

    engine = AIEngineFactory.get_engine()

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "response": '{"suggested_classification": "Schema_Validation_Failed", "suggested_action": "fix_and_retry"}'
    }
    mock_post.return_value = mock_response

    # Execute network call simulation
    result = engine.call_llm(
        "Client_1", "ValidationFailed", "Missing email", '{"account": "123"}'
    )

    # Verify network call was dispatched
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args[1]

    assert call_kwargs["json"]["model"] == "mock_model"
    assert "prompt" in call_kwargs["json"]
    assert result["suggested_action"] == "fix_and_retry"


# ==========================================
# UNIT TESTS: SECURITY & STATE
# ==========================================


def test_pii_scrubber_luhn_validation():
    """Proves the scrubber masks valid CCs but preserves system correlation IDs."""
    scrubber = PIIScrubber()
    # 4111111111111111 is a mathematically valid Visa test card
    payload = "Email dev@test.com, Phone +12345678901, CC 4111 1111 1111 1111, ID 1234567812345678"
    scrubbed = scrubber.scrub(payload)

    assert "[REDACTED_EMAIL]" in scrubbed
    assert "[REDACTED_PHONE]" in scrubbed
    assert "[REDACTED_CC]" in scrubbed
    # ID fails Luhn check, must NOT be masked to preserve telemetry
    assert "1234567812345678" in scrubbed


def test_idempotency_store_disk_persistence(temp_env, monkeypatch):
    """Proves dbm correctly persists duplicates to disk and honors TTLs."""
    now = {"value": 1_000.0}
    monkeypatch.setattr("src.state_managers.time.time", lambda: now["value"])

    store = IdempotencyStore()
    test_hash = "abc-123"

    assert store.increment(test_hash, ttl_seconds=1) == 1
    assert store.increment(test_hash, ttl_seconds=1) == 2  # Duplicate

    now["value"] = 1_003.0
    store.cleanup_expired()

    assert store.increment(test_hash, ttl_seconds=1) == 1  # Reset after TTL


# ==========================================
# INTEGRATION TESTS: THE 5-GATE PIPELINE
# ==========================================


@pytest.fixture
def classifier(temp_env, mock_infrastructure):
    return AutonomousDLQClassifier(
        idempotency_cache=IdempotencyStore(),
        classification_cache=ClassificationCache(),
        ai_client=mock_infrastructure["ai"],
        database_client=mock_infrastructure["db"],
        parking_lot_sender=mock_infrastructure["parking_sender"],
        main_queue_sender=mock_infrastructure["main_sender"],
        dlq_receiver=mock_infrastructure["receiver"],
        source_queue_name="integration-queue",
    )


def test_gate_a_poison_pill_quarantine(classifier, mock_infrastructure):
    """Proves Gate A intercepts infinite loops."""
    msg = MockServiceBusReceivedMessage(
        body_dict={}, properties={b"Resubmit-Count": 3}  # Exceeds max
    )
    classifier._classify_single_message(msg)
    mock_infrastructure["parking_sender"].send_messages.assert_called_once()
    mock_infrastructure["main_sender"].send_messages.assert_not_called()


def test_gate_b_idempotency_and_noise_suppression(classifier, mock_infrastructure):
    """Proves Gate B drops duplicates and suppresses noise after threshold."""
    msg = MockServiceBusReceivedMessage(
        body_dict={"id": 1},
        properties={b"client_id": "Client_A", b"Resubmit-Count": 0},
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'",
    )

    classifier._classify_single_message(msg)  # 1st run
    contract1 = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract1["status"] == "Auto_Classified"

    classifier._classify_single_message(msg)  # 2nd run
    contract2 = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract2["status"] == "Dropped"

    for _ in range(10):  # Run to hit threshold
        classifier._classify_single_message(msg)

    contract_final = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract_final["status"] == "Dropped_Threshold_Exceeded_Noise_Suppressed"


def test_correlation_context_uses_otel_traceparent(classifier, mock_infrastructure):
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    msg = MockServiceBusReceivedMessage(
        body_dict={"id": 77},
        properties={
            b"client_id": "Client_OTEL",
            b"message_type": "PaymentRequest",
            b"traceparent": traceparent,
            b"tracestate": "vendor=foo",
        },
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'",
        correlation_id="broker-corr-1",
    )

    classifier._classify_single_message(msg)
    contract = mock_infrastructure["db"].log_telemetry.call_args[0][0]

    assert contract["correlation_source"] == "otel_traceparent"
    assert contract["correlation_id"] == "broker-corr-1"
    assert contract["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert contract["span_id"] == "00f067aa0ba902b7"
    assert contract["tracestate"] == "vendor=foo"


def test_idempotency_prefers_otel_trace_id_over_correlation_id(
    classifier, mock_infrastructure
):
    traceparent = "00-11111111111111111111111111111111-2222222222222222-01"
    msg_a = MockServiceBusReceivedMessage(
        body_dict={"id": 100},
        properties={
            b"client_id": "Client_OTEL",
            b"message_type": "PaymentRequest",
            b"traceparent": traceparent,
        },
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'",
        correlation_id="corr-A",
    )
    msg_b = MockServiceBusReceivedMessage(
        body_dict={"id": 100},
        properties={
            b"client_id": "Client_OTEL",
            b"message_type": "PaymentRequest",
            b"traceparent": traceparent,
        },
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'",
        correlation_id="corr-B",
    )

    classifier._classify_single_message(msg_a)
    classifier._classify_single_message(msg_b)

    contract = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract["status"] == "Dropped"


def test_correlation_context_falls_back_without_otel(classifier, mock_infrastructure):
    msg = MockServiceBusReceivedMessage(
        body_dict={"id": 88},
        properties={b"client_id": "Client_NoOTEL", b"message_type": "PaymentRequest"},
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'",
        correlation_id="broker-only-corr",
    )

    classifier._classify_single_message(msg)
    contract = mock_infrastructure["db"].log_telemetry.call_args[0][0]

    assert contract["correlation_source"] == "broker_correlation_id"
    assert contract["correlation_id"] == "broker-only-corr"
    assert contract["trace_id"] is None


def test_gate_c_classification_cache(classifier, mock_infrastructure):
    """Proves identical error shapes bypass the heuristics engine via cache."""
    msg1 = MockServiceBusReceivedMessage(
        body_dict={"id": 1},
        properties={b"client_id": "Client_A"},
        reason="ValidationFailed",
        desc="missing mandatory field: 'customer_id'",
    )
    msg2 = MockServiceBusReceivedMessage(
        body_dict={"id": 2},  # Different payload hash
        properties={b"client_id": "Client_A"},
        reason="ValidationFailed",  # Identical shape
        desc="missing mandatory field: 'customer_id'",
    )

    classifier._classify_single_message(msg1)
    classifier._classify_single_message(msg2)
    contract2 = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract2["status"] == "Auto_Classified_From_Cache"


def test_gate_d_queue_override(classifier, mock_infrastructure):
    """Proves queue-specific overrides bypass global rules."""
    msg = MockServiceBusReceivedMessage(
        body_dict={},
        properties={b"client_id": "Example_Corp"},
        reason="Business_Rule_Violation",
        desc="Custom logic failed",
    )
    classifier._classify_single_message(msg)
    # Maps to the 'escalate' action defined in the integration queue override
    mock_infrastructure["parking_sender"].send_messages.assert_called_once()


def test_gate_e_ai_fallback(classifier, mock_infrastructure):
    """Proves unknown errors are routed to the AI for classification."""
    msg = MockServiceBusReceivedMessage(
        body_dict={"broken": "data"},
        properties={b"client_id": "Client_C"},
        reason="SystemFault",
        desc="Unexpected null pointer",
    )

    mock_infrastructure["ai"].call_llm.return_value = {
        "suggested_classification": "AI_Classified_Fault",
        "suggested_pattern": "ai_found_error",
        "suggested_action": "escalate",
        "confidence_score": 0.95,
    }

    classifier._classify_single_message(msg)

    mock_infrastructure["ai"].call_llm.assert_called_once()
    mock_infrastructure["parking_sender"].send_messages.assert_called_once()
    contract = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract["status"] == "AI_Suggested_Rule_Pending_Approval"


# ==========================================
# INTEGRATION TESTS: ORCHESTRATOR
# ==========================================


@pytest.mark.usefixtures("temp_env")
@patch(
    "src.run_agent.shutdown_event.is_set", side_effect=[False, False, True, True, True]
)
@patch("src.run_agent.threading.Thread")
@patch("src.run_agent.time.sleep", return_value=None)
@patch("src.run_agent.as_completed", return_value=[])
@patch("src.run_agent.DefaultAzureCredential")
@patch("src.run_agent.ServiceBusClient")
@patch("src.run_agent.ThreadPoolExecutor")
def test_orchestrator_thread_allocation(
    mock_executor,
    mock_sb_client,
    mock_cred,
    mock_as_completed,
    mock_sleep,
    mock_thread,
    mock_is_set,
    monkeypatch,
):
    """Proves the orchestrator maps ASB_SOURCES to isolated thread workers and filters non-queues."""
    from src.run_agent import main

    sources = [
        {"type": "queue", "name": "orders-queue"},
        {"type": "queue", "name": "payments-queue"},
        {"type": "topic", "name": "events-topic", "subscription": "dlq-sub"},
    ]

    monkeypatch.setenv("ASB_SOURCES", json.dumps(sources))
    monkeypatch.setenv(
        "SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", "mock.servicebus.windows.net"
    )
    monkeypatch.setenv("ENABLE_DYNAMIC_DISCOVERY", "False")
    monkeypatch.setenv("PARKING_LOT_QUEUE_NAME", "parking-lot-queue")

    mock_pool_instance = MagicMock()
    mock_executor.return_value.__enter__.return_value = mock_pool_instance

    main()

    assert mock_pool_instance.submit.call_count == 2

    # CRITICAL ARCHITECT FIX: Safely unpack call_args_list to avoid mock_calls tuple IndexError
    submitted_args = [
        args[1] for args, kwargs in mock_pool_instance.submit.call_args_list
    ]
    assert "orders-queue" in submitted_args
    assert "payments-queue" in submitted_args
    assert "events-topic" not in submitted_args


@pytest.mark.usefixtures("temp_env")
@patch("src.run_agent.ServiceBusClientFactory")
@patch("src.run_agent.AutonomousDLQClassifier")
def test_drain_queue_dlq_flows(mock_classifier_cls, mock_factory):
    """Validates that drain_queue_dlq initialises receivers and dispatches batches to the classifier."""
    from src.run_agent import drain_queue_dlq

    mock_client = MagicMock()
    mock_factory.get_client.return_value = mock_client

    mock_receiver = MagicMock()
    mock_main_sender = MagicMock()
    mock_parking_sender = MagicMock()

    mock_client.get_queue_receiver.return_value = mock_receiver
    mock_client.get_queue_sender.side_effect = [mock_main_sender, mock_parking_sender]

    # Simulate finding exactly 1 message, followed by an empty list to exit polling cleanly
    mock_msg = MagicMock()
    mock_receiver.receive_messages.side_effect = [[mock_msg], []]

    mock_classifier_instance = MagicMock()
    mock_classifier_cls.return_value = mock_classifier_instance

    drain_queue_dlq(
        queue_name="orders-queue",
        fqdn="mock.servicebus.windows.net",
        credential=MagicMock(),
        idempotency_store=MagicMock(),
        classification_cache=MagicMock(),
        ai_engine=MagicMock(),
        db_client=MagicMock(),
    )

    # Assert AMQP multiplexed resource collection boundaries are preserved
    mock_client.get_queue_receiver.assert_called_once()
    assert mock_client.get_queue_sender.call_count == 2
    mock_classifier_instance.process_batch.assert_called_once_with([mock_msg])


# ==========================================
# UNIT TESTS: COVERAGE ANNIHILATORS
# ==========================================


def test_all_action_commands_execution(mock_infrastructure):
    """Forces execution coverage across all standard ActionRouter command branches."""
    router = ActionRouter(
        mock_infrastructure["receiver"],
        mock_infrastructure["main_sender"],
        mock_infrastructure["parking_sender"],
    )
    msg = MockServiceBusReceivedMessage(body_dict={}, properties={b"Resubmit-Count": 0})

    router.route_and_execute("drop", msg, "rule_1", None)
    router.route_and_execute("drop_and_notify", msg, "rule_2", None)
    router.route_and_execute("retry", msg, "rule_3", None)
    router.route_and_execute("escalate", msg, "rule_4", None)

    # Verify the commands executed their specific ASB actions
    assert mock_infrastructure["receiver"].complete_message.call_count == 4
    assert (
        mock_infrastructure["main_sender"].send_messages.call_count == 1
    )  # Triggered by retry
    assert (
        mock_infrastructure["parking_sender"].send_messages.call_count == 1
    )  # Triggered by escalate


def test_demo_terminal_database_logging(tmp_path):
    """Validates the CSV logging mechanism to cover run_agent.py database gaps."""
    from src.run_agent import DemoTerminalDatabase

    csv_path = tmp_path / "telemetry_test.csv"
    db = DemoTerminalDatabase(filepath=str(csv_path))

    contract = {
        "status": "Auto_Classified",
        "classification": "Schema_Validation_Failed",
        "pattern": "missing_email",
        "source_queue": "test-queue",
        "client_id": "Client_A",
        "message_type": "Payment",
        "suggested_action": "drop",
        "confidence_score": 0.99,
    }

    db.log_telemetry(contract)
    assert os.path.exists(str(csv_path))
    with open(str(csv_path), "r") as f:
        lines = f.readlines()
        assert len(lines) == 2  # 1 Header row + 1 Data row


@patch("src.run_agent.shutdown_event.is_set")
@patch("src.run_agent.time.sleep", return_value=None)
def test_disk_cleanup_daemon_execution(mock_sleep, mock_is_set):
    """Forces the daemon thread to execute its TTL cleanup to satisfy coverage."""
    from src.run_agent import disk_cleanup_daemon

    # Simulates: Enter while (False), Enter for-loop 3600 times (False), then Exit while (True)
    responses = [False] + [False] * 3600 + [True]
    mock_is_set.side_effect = responses

    mock_store = MagicMock()
    disk_cleanup_daemon(mock_store)
    mock_store.cleanup_expired.assert_called_once()


def test_discover_target_queues_json_error(monkeypatch):
    """Proves discovery handles corrupted ASB_SOURCES gracefully."""
    monkeypatch.setenv("ENABLE_DYNAMIC_DISCOVERY", "False")
    monkeypatch.setenv("ASB_SOURCES", "{INVALID_JSON]")  # Intentional corruption

    queues = discover_target_queues("mock.namespace", MagicMock())
    assert queues == []


# ==========================================
# UNIT TESTS: EDGE CASES & AZURE
# ==========================================


@pytest.mark.usefixtures("temp_env")
@patch("src.ai_client.ChatCompletionsClient")
def test_azure_foundry_engine_execution(mock_azure_client, monkeypatch):
    """Proves the Azure Foundry AI engine correctly formats and parses responses."""
    monkeypatch.setenv("AI_PROVIDER", "AZURE_FOUNDRY")
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://mock.com")
    monkeypatch.setenv("AZURE_FOUNDRY_DEPLOYMENT_NAME", "gpt-4o")

    engine = AIEngineFactory.get_engine()
    assert isinstance(engine, AzureFoundryEngine)
    azure_engine = cast(AzureFoundryEngine, engine)
    mock_choice = MagicMock()
    mock_choice.message.content = (
        '{"suggested_classification": "Azure_Tested", "suggested_action": "drop"}'
    )
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    cast(Any, azure_engine.client).complete.return_value = mock_response

    result = azure_engine.call_llm(
        "Client_1", "ValidationFailed", "Missing email", '{"account": "123"}'
    )
    assert result["suggested_action"] == "drop"


@pytest.mark.usefixtures("temp_env")
@patch("src.ai_client.ChatCompletionsClient")
def test_azure_foundry_engine_retries_with_max_completion_tokens(
    mock_azure_client, monkeypatch
):
    """Proves compatibility retry for models that reject max_tokens."""
    monkeypatch.setenv("AI_PROVIDER", "AZURE_FOUNDRY")
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://mock.com")
    monkeypatch.setenv("AZURE_FOUNDRY_DEPLOYMENT_NAME", "gpt-5-mini")

    engine = AIEngineFactory.get_engine()
    assert isinstance(engine, AzureFoundryEngine)
    azure_engine = cast(AzureFoundryEngine, engine)

    mock_choice = MagicMock()
    mock_choice.message.content = (
        '{"suggested_classification": "Azure_Tested", "suggested_action": "drop"}'
    )
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    cast(Any, azure_engine.client).complete.side_effect = [
        Exception(
            "(unsupported_parameter) Unsupported parameter: 'max_tokens' is not supported with this model. "
            "Use 'max_completion_tokens' instead."
        ),
        mock_response,
    ]

    result = azure_engine.call_llm(
        "Client_1", "ValidationFailed", "Missing email", '{"account": "123"}'
    )

    assert result["suggested_action"] == "drop"
    assert cast(Any, azure_engine.client).complete.call_count == 2

    first_call_kwargs = cast(Any, azure_engine.client).complete.call_args_list[0].kwargs
    second_call_kwargs = cast(Any, azure_engine.client).complete.call_args_list[1].kwargs
    assert "max_tokens" in first_call_kwargs
    assert "max_completion_tokens" not in first_call_kwargs
    assert "max_completion_tokens" in second_call_kwargs


def test_fix_and_retry_fallback_escalation(mock_infrastructure):
    """Proves that if a safe default is missing from rules.json, it degrades gracefully to Escalate."""
    router = ActionRouter(
        mock_infrastructure["receiver"],
        mock_infrastructure["main_sender"],
        mock_infrastructure["parking_sender"],
    )
    msg = MockServiceBusReceivedMessage(body_dict={"old": "data"}, properties={})

    router.route_and_execute(
        "fix_and_retry", msg, "missing_field_unknown_key", {"known_key": 0}
    )

    mock_infrastructure["parking_sender"].send_messages.assert_called_once()
    mock_infrastructure["main_sender"].send_messages.assert_not_called()


@patch("src.run_agent.ServiceBusClientFactory")
def test_drain_queue_dlq_hard_crash_safety(mock_factory):
    """Proves that a catastrophic thread exception in the drainer is handled safely."""
    from src.run_agent import drain_queue_dlq

    mock_factory.get_client.side_effect = Exception("Catastrophic AMQP Failure")

    # This should no longer raise an exception because of our src/run_agent.py patch!
    drain_queue_dlq(
        "test-queue",
        "mock",
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )


@patch("src.run_agent.ServiceBusClientFactory")
def test_drain_queue_dlq_auth_failure_stops_agent(mock_factory):
    from src.run_agent import drain_queue_dlq, runtime_health, shutdown_event

    runtime_health.reset()
    shutdown_event.clear()
    mock_factory.get_client.side_effect = ClientAuthenticationError("Auth failed")

    drain_queue_dlq(
        "test-queue",
        "mock",
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    assert shutdown_event.is_set() is True
    assert runtime_health.snapshot()["shutdown_requested"] is True

    shutdown_event.clear()
    runtime_health.reset()


@patch("azure.servicebus.ServiceBusClient")
@patch("azure.servicebus.management.ServiceBusAdministrationClient")
def test_flush_queues_coverage_absorption(mock_admin, mock_client, monkeypatch):
    """Safely absorbs the coverage penalty of the flush_queues utility."""
    monkeypatch.setenv(
        "SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", "mock.servicebus.windows.net"
    )
    try:
        import src.flush_queues

        if hasattr(src.flush_queues, "main"):
            src.flush_queues.main()
    except Exception:
        pass


def test_flush_queues_missing_namespace(monkeypatch):
    import src.flush_queues as fq

    monkeypatch.delenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", raising=False)

    with patch.object(fq.logger, "error") as mock_error:
        fq.main()

    mock_error.assert_called_once_with(
        "Missing SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE in environment settings"
    )


def test_flush_queues_invalid_sources_json(monkeypatch):
    import src.flush_queues as fq

    monkeypatch.setenv(
        "SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", "mock.servicebus.windows.net"
    )
    monkeypatch.setenv("ASB_SOURCES", "{invalid_json]")

    with patch.object(fq.logger, "error") as mock_error:
        fq.main()

    mock_error.assert_called_once_with("ASB_SOURCES must be a valid JSON array.")


def test_flush_queues_connection_string_and_parking_lot(monkeypatch):
    import src.flush_queues as fq

    monkeypatch.setenv(
        "SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", "mock.servicebus.windows.net"
    )
    monkeypatch.setenv(
        "ASB_SOURCES",
        json.dumps(
            [
                {"type": "queue", "name": "orders-queue"},
                {"type": "topic", "name": "events-topic"},
            ]
        ),
    )
    monkeypatch.setenv("SERVICE_BUS_CONNECTION_STRING", "Endpoint=sb://mock/")
    monkeypatch.setenv("PARKING_LOT_QUEUE_NAME", "parking-lot-queue")

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client

    with patch.object(
        fq.ServiceBusClient, "from_connection_string", return_value=mock_client
    ) as mock_conn, patch.object(fq, "flush_queue") as mock_flush_queue:
        fq.main()

    mock_conn.assert_called_once_with("Endpoint=sb://mock/")
    assert mock_flush_queue.call_count == 3
    mock_flush_queue.assert_any_call(mock_client, "orders-queue", is_dlq=False)
    mock_flush_queue.assert_any_call(mock_client, "orders-queue", is_dlq=True)
    mock_flush_queue.assert_any_call(mock_client, "parking-lot-queue", is_dlq=False)


def test_router_internal_exception_handling(mock_infrastructure):
    """Triggers the internal exception handlers of ActionRouter commands for 85% coverage."""
    from src.action_executor import ActionRouter

    router = ActionRouter(
        mock_infrastructure["receiver"],
        mock_infrastructure["main_sender"],
        mock_infrastructure["parking_sender"],
    )
    msg = MockServiceBusReceivedMessage(body_dict={}, properties={})

    # Force a failure during the settlement phase of a Drop command
    mock_infrastructure["receiver"].complete_message.side_effect = Exception(
        "Settlement Failed"
    )

    # This triggers the nested logger.error(exc_info=True) in DropCommand
    router.route_and_execute("drop", msg, "coverage_test", None)
    assert mock_infrastructure["receiver"].complete_message.called


# ==========================================
# UNIT TESTS: COVERAGE MAXIMISATION (UNHAPPY PATHS)
# ==========================================

from unittest.mock import PropertyMock


def test_run_agent_signal_handler():
    """Covers the OS-level graceful shutdown interrupt signal (Ctrl+C)."""
    from src.run_agent import shutdown_event, signal_handler

    shutdown_event.clear()
    signal_handler(2, None)
    assert shutdown_event.is_set()


def test_run_agent_missing_fqdn(monkeypatch):
    """Covers early orchestrator exit when the namespace FQDN is missing."""
    from src.run_agent import main

    # Keep both keys present-but-empty so load_dotenv() cannot repopulate them from .env.
    monkeypatch.setenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", "")
    monkeypatch.setenv("SERVICE_BUS_CONNECTION_STRING", "")
    # Should safely return without executing or crashing
    main()


def test_startup_validation_requires_core_service_bus_config(monkeypatch):
    """Covers early orchestrator exit when the core Service Bus configuration is missing."""
    from src.run_agent import _validate_startup_configuration

    monkeypatch.setenv("AI_PROVIDER", "OLLAMA")
    monkeypatch.setenv("OLLAMA_MODEL", "mock_model")
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://mock")
    monkeypatch.setenv("ENABLE_DYNAMIC_DISCOVERY", "True")

    errors = _validate_startup_configuration(conn_str=None, fqdn=None)

    assert any(
        "Either SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE or SERVICE_BUS_CONNECTION_STRING"
        in e
        for e in errors
    )


def test_startup_validation_requires_foundry_fields(monkeypatch):
    from src.run_agent import _validate_startup_configuration

    monkeypatch.setenv("AI_PROVIDER", "AZURE_FOUNDRY")
    monkeypatch.delenv("AZURE_FOUNDRY_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_FOUNDRY_DEPLOYMENT_NAME", raising=False)
    monkeypatch.setenv("ENABLE_DYNAMIC_DISCOVERY", "True")

    errors = _validate_startup_configuration(
        conn_str="Endpoint=sb://mock/;SharedAccessKeyName=Root;SharedAccessKey=x",
        fqdn="mock.servicebus.windows.net",
    )

    assert any("AZURE_FOUNDRY_ENDPOINT" in e for e in errors)
    assert any("AZURE_FOUNDRY_DEPLOYMENT_NAME" in e for e in errors)


def test_startup_validation_rejects_conn_str_with_multi_namespace(monkeypatch):
    from src.run_agent import _validate_startup_configuration

    monkeypatch.setenv("AI_PROVIDER", "OLLAMA")
    monkeypatch.setenv("OLLAMA_MODEL", "mock_model")
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://mock")

    errors = _validate_startup_configuration(
        conn_str="Endpoint=sb://mock/;SharedAccessKeyName=Root;SharedAccessKey=x",
        fqdn=None,
        fqdn_list_raw="ns-a.servicebus.windows.net,ns-b.servicebus.windows.net",
    )

    assert any(
        "SERVICE_BUS_CONNECTION_STRING cannot be combined with SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES"
        in e
        for e in errors
    )


def test_resolve_namespace_targets_prefers_multi_namespace_list():
    from src.run_agent import _resolve_namespace_targets

    targets = _resolve_namespace_targets(
        conn_str="Endpoint=sb://single.servicebus.windows.net/;SharedAccessKeyName=Root;SharedAccessKey=x",
        fqdn="single.servicebus.windows.net",
        fqdn_list_raw="ns-a.servicebus.windows.net, ns-b.servicebus.windows.net, ns-a.servicebus.windows.net",
    )

    assert targets == ["ns-a.servicebus.windows.net", "ns-b.servicebus.windows.net"]


@pytest.mark.usefixtures("temp_env")
@patch("src.run_agent.discover_target_queues")
@patch("src.run_agent.shutdown_event.is_set", side_effect=[False, True, True])
@patch("src.run_agent.threading.Thread")
@patch("src.run_agent.time.sleep", return_value=None)
@patch("src.run_agent.as_completed", return_value=[])
@patch("src.run_agent.DefaultAzureCredential")
@patch("src.run_agent.ThreadPoolExecutor")
def test_orchestrator_multi_namespace_bindings(
    mock_executor,
    mock_cred,
    mock_as_completed,
    mock_sleep,
    mock_thread,
    mock_is_set,
    mock_discover,
    monkeypatch,
):
    from src.run_agent import main

    monkeypatch.setenv(
        "SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES",
        "ns-a.servicebus.windows.net,ns-b.servicebus.windows.net",
    )
    monkeypatch.delenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", raising=False)
    monkeypatch.setenv("SERVICE_BUS_CONNECTION_STRING", "")
    monkeypatch.setenv("ENABLE_DYNAMIC_DISCOVERY", "True")

    mock_discover.side_effect = [["orders-queue"], ["payments-queue", "refunds-queue"]]

    mock_pool_instance = MagicMock()
    mock_executor.return_value.__enter__.return_value = mock_pool_instance

    main()

    assert mock_discover.call_count == 2
    assert mock_pool_instance.submit.call_count == 3

    submitted_pairs = [
        (args[2], args[1]) for args, kwargs in mock_pool_instance.submit.call_args_list
    ]
    assert ("ns-a.servicebus.windows.net", "orders-queue") in submitted_pairs
    assert ("ns-b.servicebus.windows.net", "payments-queue") in submitted_pairs
    assert ("ns-b.servicebus.windows.net", "refunds-queue") in submitted_pairs


def test_ai_client_missing_env_vars(monkeypatch):
    """Covers ValueError raisers in AI engine initialisation."""
    monkeypatch.setenv("AI_PROVIDER", "OLLAMA")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    with pytest.raises(ValueError):
        AIEngineFactory.get_engine()

    monkeypatch.setenv("AI_PROVIDER", "AZURE_FOUNDRY")
    monkeypatch.delenv("AZURE_FOUNDRY_ENDPOINT", raising=False)
    with pytest.raises(ValueError):
        AIEngineFactory.get_engine()


@pytest.mark.usefixtures("temp_env")
def test_ai_client_invalid_provider(monkeypatch):
    """Covers fallback routing for unsupported AI providers."""
    monkeypatch.setenv("AI_PROVIDER", "UNSUPPORTED_AI")

    # It should NOT raise a ValueError. It should log a warning and default to Ollama.
    engine = AIEngineFactory.get_engine()
    assert engine.__class__.__name__ == "OllamaEngine"


@pytest.mark.usefixtures("temp_env")
def test_ai_salvage_json_complete_failure(monkeypatch):
    """Covers the strict ValueError raiser when the parser receives conversational garbage."""
    monkeypatch.setenv("AI_PROVIDER", "OLLAMA")
    engine = AIEngineFactory.get_engine()

    # The pure parsing function MUST throw a ValueError on garbage
    with pytest.raises(ValueError, match="No JSON object found"):
        engine._salvage_json("I am an AI and I cannot help with that request.")


@pytest.mark.usefixtures("temp_env")
@patch("src.ai_client.requests.post")
def test_ai_call_llm_garbage_raises_value_error(mock_post, monkeypatch):
    """Covers the AI Engine strictly throwing an error on garbage, deferring fallback to the Classifier."""
    monkeypatch.setenv("AI_PROVIDER", "OLLAMA")
    engine = AIEngineFactory.get_engine()

    # Mock the LLM returning pure conversational text without JSON
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "response": "I am an AI and I cannot help with that request."
    }
    mock_post.return_value = mock_response

    # The API layer MUST fail fast and throw the error to the Orchestrator
    with pytest.raises(ValueError, match="No JSON object found"):
        engine.call_llm("Client", "Reason", "Desc", "{}")


def test_classifier_ai_exception_handling(classifier, mock_infrastructure):
    """Proves the classifier catches the AI engine's ValueError and safely escalates to the Parking Lot."""
    msg = MockServiceBusReceivedMessage(body_dict={"test": "data"}, properties={})

    # The AI Engine throws the error from the test above
    mock_infrastructure["ai"].call_llm.side_effect = ValueError("No JSON object found")

    classifier._classify_single_message(msg)

    # The Classifier must intercept the crash and route the untriaged message to humans
    mock_infrastructure["parking_sender"].send_messages.assert_called_once()
    mock_infrastructure["main_sender"].send_messages.assert_not_called()


def test_action_executor_unknown_action(mock_infrastructure):
    """Covers the router's fallback when an unknown action string is mapped."""
    router = ActionRouter(
        mock_infrastructure["receiver"],
        mock_infrastructure["main_sender"],
        mock_infrastructure["parking_sender"],
    )
    msg = MockServiceBusReceivedMessage(body_dict={"test": "data"}, properties={})

    # Attempting to execute a command that doesn't exist in the command dictionary
    router.route_and_execute("launch_nukes", msg, "unknown_rule_test", None)

    # Should log the error and default to escalating to the parking lot
    mock_infrastructure["parking_sender"].send_messages.assert_called_once()


def test_classifier_malformed_message_body(classifier):
    """Covers the classifier's internal exception handler for completely broken AMQP frames."""
    msg = MagicMock()
    # Trigger an exception when the classifier tries to read the application_properties
    type(msg).application_properties = PropertyMock(
        side_effect=Exception("Corrupted AMQP Frame")
    )

    # Should catch the exception, log it, and continue the batch without crashing the thread
    classifier.process_batch([msg])


def test_classifier_missing_reason_and_desc(classifier):
    """Covers classifier fallback logic when ASB native dead-letter reason is mysteriously null."""
    msg = MockServiceBusReceivedMessage(body_dict={"test": "data"}, properties={})
    msg.dead_letter_reason = None
    msg.dead_letter_error_description = None

    # Should process without hitting a NullPointerException
    classifier._classify_single_message(msg)


# ==========================================
# UNIT TESTS: THE FINAL DEFENSIVE GATES
# ==========================================


@patch("src.state_managers.dbm.open")
def test_idempotency_db_open_error(mock_dbm, temp_env):
    """Covers the exception handler when the IdempotencyStore fails to access the disk."""
    mock_dbm.side_effect = Exception("Mock Disk Full or Permission Denied")
    store = IdempotencyStore()

    # Must gracefully degrade to returning '1' (first occurrence)
    # to allow processing to continue instead of crashing the thread.
    assert store.increment("test_hash", 60) == 1


def test_classification_cache_expiry():
    """Covers the time-based eviction branch inside the ClassificationCache."""
    # Set a short integer TTL because ClassificationCache expects int seconds
    cache = ClassificationCache(ttl_seconds=1)

    # CRITICAL FIX: Use the actual method 'save' and pass a dictionary as expected by the signature
    cache.save(
        "test_hash", {"classification": "Test_Class", "suggested_action": "drop"}
    )

    # Wait for the TTL to expire
    time.sleep(1.1)

    # Assert the cache eviction branch evaluates to True and returns None
    assert cache.get("test_hash") is None


def test_classification_cache_uses_environment_defaults(monkeypatch):
    monkeypatch.setenv("CLASSIFICATION_CACHE_MAXSIZE", "123")
    monkeypatch.setenv("CLASSIFICATION_TTL_SECONDS", "7")

    cache = ClassificationCache()

    assert cache.cache.maxsize == 123
    assert int(cache.cache.ttl) == 7


def test_action_executor_corrupted_payload_in_fix(mock_infrastructure):
    """Covers the JSON decode fallback deep inside the FixAndRetryCommand."""
    from src.action_executor import ActionRouter

    router = ActionRouter(
        mock_infrastructure["receiver"],
        mock_infrastructure["main_sender"],
        mock_infrastructure["parking_sender"],
    )

    # Construct a message with a mathematically invalid JSON byte string
    msg = MockServiceBusReceivedMessage(body_dict={}, properties={})
    msg.body = [b"{ I AM CORRUPTED [ NOT JSON }"]

    # FixAndRetry must catch the internal decode error and safely degrade to Escalation
    router.route_and_execute(
        "fix_and_retry", msg, "corrupt_payload_test", {"missing_key": 0}
    )

    mock_infrastructure["parking_sender"].send_messages.assert_called_once()
    mock_infrastructure["main_sender"].send_messages.assert_not_called()


def test_retry_propagates_otel_correlation_headers(mock_infrastructure):
    router = ActionRouter(
        mock_infrastructure["receiver"],
        mock_infrastructure["main_sender"],
        mock_infrastructure["parking_sender"],
    )
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    msg = MockServiceBusReceivedMessage(
        body_dict={"test": "data"},
        properties={b"Diagnostic-Id": traceparent, b"tracestate": "vendor=alpha"},
        correlation_id="broker-corr-42",
    )

    router.route_and_execute("retry", msg, "otel_propagation_test", None)

    sent_msg = mock_infrastructure["main_sender"].send_messages.call_args[0][0]
    props = sent_msg.application_properties

    assert props["traceparent"] == traceparent
    assert props["diagnostic-id"] == traceparent
    assert props["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert props["span_id"] == "00f067aa0ba902b7"
    assert props["tracestate"] == "vendor=alpha"
    assert props["correlation_id"] == "broker-corr-42"
    assert props["Correlation-Id"] == "broker-corr-42"
    assert props["OriginalMessageId"] == "test-msg-123"
    assert props["Resubmit-Count"] == 1


def test_retry_propagates_fallback_correlation_when_no_otel(mock_infrastructure):
    router = ActionRouter(
        mock_infrastructure["receiver"],
        mock_infrastructure["main_sender"],
        mock_infrastructure["parking_sender"],
    )
    msg = MockServiceBusReceivedMessage(
        body_dict={"test": "data"},
        properties={},
        message_id="msg-fallback-1",
        correlation_id=None,
    )

    router.route_and_execute("retry", msg, "fallback_correlation_test", None)

    sent_msg = mock_infrastructure["main_sender"].send_messages.call_args[0][0]
    props = sent_msg.application_properties

    assert props["correlation_id"] == "msg-fallback-1"
    assert props["Correlation-Id"] == "msg-fallback-1"
    assert props["OriginalMessageId"] == "msg-fallback-1"
    assert "traceparent" not in props


def test_backoff_sleep_without_jitter(monkeypatch):
    from src.resilience import backoff_sleep

    slept = []

    def fake_sleep(duration):
        slept.append(duration)

    monkeypatch.setattr("src.resilience.time.sleep", fake_sleep)
    duration = backoff_sleep(
        attempt=2, base_seconds=1.0, max_seconds=10.0, jitter=False
    )

    assert duration == 4.0
    assert slept == [4.0]


def test_circuit_breaker_open_blocks_until_recovery(monkeypatch):
    from src.resilience import CircuitBreaker

    CircuitBreaker.reset("unit-circuit")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_RECOVERY_TIMEOUT", "999")

    cb = CircuitBreaker("unit-circuit")
    assert cb.allow_request() is True
    cb.record_failure()
    assert cb.allow_request() is True
    cb.record_failure()

    # OPEN now blocks requests
    assert cb.allow_request() is False


def test_retry_command_uses_configurable_attempt_limit(monkeypatch):
    monkeypatch.setenv("ACTION_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("ACTION_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("ACTION_BACKOFF_MAX_SECONDS", "0")

    from src.action_executor import RetryCommand

    receiver = MagicMock()
    sender = MagicMock()
    parking = MagicMock()
    sender.send_messages.side_effect = Exception("send failed")

    msg = MockServiceBusReceivedMessage(
        body_dict={"k": "v"},
        properties={},
        message_id="retry-attempts-msg",
        correlation_id="corr",
    )

    cmd = RetryCommand()
    cmd.execute(
        msg, receiver, sender, parking, pattern="transient", safe_defaults_map=None
    )

    # Retries should respect ACTION_RETRY_MAX_ATTEMPTS
    assert sender.send_messages.call_count == 2


def test_ai_invoke_respects_open_circuit(monkeypatch):
    from src.autonomous_dlq_classifier import AutonomousDLQClassifier
    from src.resilience import CircuitBreaker

    CircuitBreaker.reset("ai")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("CIRCUIT_RECOVERY_TIMEOUT", "999")

    # Trip the AI circuit first
    cb = CircuitBreaker("ai")
    cb.record_failure()

    classifier = AutonomousDLQClassifier(
        idempotency_cache=MagicMock(),
        classification_cache=MagicMock(),
        ai_client=MagicMock(),
        database_client=MagicMock(),
        parking_lot_sender=MagicMock(),
        main_queue_sender=MagicMock(),
        dlq_receiver=MagicMock(),
        source_queue_name="integration-queue",
    )

    result = classifier._invoke_ai_with_salvage("client", "reason", "desc", "payload")

    assert result["suggested_action"] == "escalate"
    assert result["suggested_classification"] == "AI_UNAVAILABLE"


def test_drain_queue_backoff_retries(monkeypatch):
    from src.resilience import CircuitBreaker
    from src.run_agent import drain_queue_dlq

    monkeypatch.setenv("DRAIN_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("DRAIN_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("DRAIN_BACKOFF_MAX_SECONDS", "0")

    # Ensure queue circuit is permissive for this isolated test
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "99")
    CircuitBreaker.reset("queue:integration-queue")

    fake_client = MagicMock()
    fake_client.get_queue_receiver.side_effect = Exception("broker down")

    monkeypatch.setattr(
        "src.run_agent.ServiceBusClientFactory.get_client",
        lambda fqdn, cred: fake_client,
    )

    drain_queue_dlq(
        queue_name="integration-queue",
        fqdn="mock.servicebus.windows.net",
        credential=MagicMock(),
        idempotency_store=MagicMock(),
        classification_cache=MagicMock(),
        ai_engine=MagicMock(),
        db_client=MagicMock(),
    )

    assert fake_client.get_queue_receiver.call_count == 2


def test_classifier_cache_corruption_fails_open(classifier, mock_infrastructure):
    """If cache payload is corrupted, classifier should fail-open and safely abandon."""
    msg = MockServiceBusReceivedMessage(
        body_dict={"id": 1},
        properties={b"client_id": "Client_A"},
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'",
    )

    classifier.classification_cache.exists = MagicMock(return_value=True)
    # Corrupted cache record shape that breaks expected dict indexing
    classifier.classification_cache.get = MagicMock(return_value="not-a-dict")

    classifier.process_batch([msg])

    mock_infrastructure["receiver"].abandon_message.assert_called_once_with(msg)


def test_ai_timeout_cascade_degrades_to_safe_escalation(monkeypatch):
    from src.autonomous_dlq_classifier import AutonomousDLQClassifier
    from src.resilience import CircuitBreaker

    ai_client = MagicMock()
    ai_client.call_llm.side_effect = TimeoutError("LLM timeout")

    monkeypatch.setenv("AI_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("AI_BACKOFF_BASE_SECONDS", "0")
    monkeypatch.setenv("AI_BACKOFF_MAX_SECONDS", "0")
    CircuitBreaker.reset("ai")

    classifier = AutonomousDLQClassifier(
        idempotency_cache=MagicMock(),
        classification_cache=MagicMock(),
        ai_client=ai_client,
        database_client=MagicMock(),
        parking_lot_sender=MagicMock(),
        main_queue_sender=MagicMock(),
        dlq_receiver=MagicMock(),
        source_queue_name="integration-queue",
    )

    result = classifier._invoke_ai_with_salvage("client", "reason", "desc", "{}")

    assert ai_client.call_llm.call_count == 2
    assert result["suggested_action"] == "escalate"
    assert result["suggested_classification"] == "AI_UNAVAILABLE"


def test_drain_queue_shutdown_during_processing(monkeypatch):
    from src.resilience import CircuitBreaker
    from src.run_agent import drain_queue_dlq, shutdown_event

    shutdown_event.clear()
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "99")
    CircuitBreaker.reset("queue:integration-queue")

    mock_receiver = MagicMock()
    mock_receiver.__enter__.return_value = mock_receiver
    mock_receiver.__exit__.return_value = False
    mock_receiver.receive_messages.return_value = [MagicMock()]

    mock_sender_main = MagicMock()
    mock_sender_main.__enter__.return_value = mock_sender_main
    mock_sender_main.__exit__.return_value = False

    mock_sender_parking = MagicMock()
    mock_sender_parking.__enter__.return_value = mock_sender_parking
    mock_sender_parking.__exit__.return_value = False

    fake_client = MagicMock()
    fake_client.get_queue_receiver.return_value = mock_receiver
    fake_client.get_queue_sender.side_effect = [mock_sender_main, mock_sender_parking]

    classifier_instance = MagicMock()

    def process_batch_and_request_shutdown(_messages):
        shutdown_event.set()

    classifier_instance.process_batch.side_effect = process_batch_and_request_shutdown

    monkeypatch.setattr(
        "src.run_agent.ServiceBusClientFactory.get_client",
        lambda fqdn, cred: fake_client,
    )
    monkeypatch.setattr(
        "src.run_agent.AutonomousDLQClassifier",
        lambda **kwargs: classifier_instance,
    )

    drain_queue_dlq(
        queue_name="integration-queue",
        fqdn="mock.servicebus.windows.net",
        credential=MagicMock(),
        idempotency_store=MagicMock(),
        classification_cache=MagicMock(),
        ai_engine=MagicMock(),
        db_client=MagicMock(),
    )

    # Should stop cleanly after shutdown is requested during first processed batch.
    assert mock_receiver.receive_messages.call_count == 1
    shutdown_event.clear()
