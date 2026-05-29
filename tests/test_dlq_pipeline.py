import os
import json
import time
import pytest
from unittest.mock import MagicMock, patch
from azure.servicebus.exceptions import ServiceBusError

from src.autonomous_dlq_classifier import AutonomousDLQClassifier
from src.action_executor import ActionRouter, EscalateCommand
from src.state_managers import IdempotencyStore, ClassificationCache
from src.ai_client import PIIScrubber, LocalOllamaClient

# ==========================================
# FIXTURES & MOCKS
# ==========================================

class MockServiceBusReceivedMessage:
    """Simulates an Azure Service Bus message with PEEK_LOCK state."""
    def __init__(self, body_dict, properties, reason="Unknown", desc="Unknown"):
        self.body = [json.dumps(body_dict).encode('utf-8')]
        self.application_properties = properties
        self.message_id = "test-msg-123"
        self.correlation_id = "corr-456"
        self.dead_letter_reason = reason
        self.dead_letter_error_description = desc
        self.subject = "TestSubject"
        self.content_type = "application/json"

@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Scaffolds a safe, isolated filesystem for tests."""
    db_path = tmp_path / "idempotency.db"
    rules_path = tmp_path / "rules.json"
    
    rules_data = {
        "global_rules": [
            {
                "rule_id": "app_001",
                "severity_score": 50,
                "classification": "Schema_Validation_Failed",
                "pattern_name": "validation_failed_generic",
                "condition": "reason == 'ValidationFailed'",
                "pattern_regex": "missing mandatory field: '(\\w+)'",
                "default_action": "fix_and_retry"
            }
        ],
        "queue_overrides": {
            "test-queue": [
                {
                    "rule_id": "app_override",
                    "severity_score": 10,
                    "classification": "Business_Logic_Violation",
                    "pattern_name": "tenant_specific_drop",
                    "condition": "reason == 'ValidationFailed' and client_id == 'Special_Tenant'",
                    "default_action": "drop"
                }
            ]
        }
    }
    rules_path.write_text(json.dumps(rules_data))
    
    monkeypatch.setenv("IDEMPOTENCY_DB_PATH", str(db_path))
    monkeypatch.setenv("RULES_FILE_PATH", str(rules_path))
    monkeypatch.setenv("ENABLE_NEW_MESSAGE_ID_ON_RETRY", "True")
    monkeypatch.setenv("OLLAMA_MODEL", "mock_model")
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://mock")
    
    return tmp_path

@pytest.fixture
def mock_infrastructure():
    return {
        "receiver": MagicMock(),
        "main_sender": MagicMock(),
        "parking_sender": MagicMock(),
        "db": MagicMock(),
        "ai": MagicMock()
    }

# ==========================================
# UNIT TESTS: STATE MANAGEMENT & CACHING
# ==========================================

def test_idempotency_store_disk_persistence(temp_env):
    """Proves dbm correctly persists duplicates to disk and honors TTLs."""
    store = IdempotencyStore()
    test_hash = "abc-123"
    
    # First occurrence
    assert store.increment(test_hash, ttl_seconds=1) == 1
    # Second occurrence (Duplicate)
    assert store.increment(test_hash, ttl_seconds=1) == 2
    
    # Prove TTL Expiry
    time.sleep(1.1) 
    store.cleanup_expired()
    
    # Should reset to 1 after cleanup
    assert store.increment(test_hash, ttl_seconds=1) == 1

# ==========================================
# UNIT TESTS: SECURITY & ZERO TRUST
# ==========================================

def test_pii_scrubber_luhn_validation():
    """Proves the scrubber masks valid CCs but preserves system correlation IDs."""
    scrubber = PIIScrubber()
    
    # 4111111111111111 is a mathematically valid Visa test card
    payload = 'Email dev@test.com, Phone +12345678901, CC 4111 1111 1111 1111, ID 1234567812345678'
    scrubbed = scrubber.scrub(payload)
    
    assert "[REDACTED_EMAIL]" in scrubbed
    assert "[REDACTED_PHONE]" in scrubbed
    assert "[REDACTED_CC]" in scrubbed
    # ID fails Luhn check, must NOT be masked to preserve telemetry
    assert "1234567812345678" in scrubbed 

def test_ai_json_salvage(classifier):
    """Proves regex extraction bypasses LLM conversational hallucinations."""
    
    # Using standard triple-quotes is the safest and most readable way 
    # to mock raw, multi-line LLM text outputs.
    messy_response = """Certainly! Here is the JSON:
    ```json
    {"suggested_action": "drop"}
    ```Hope this helps!"""
    parsed = classifier._salvage_json(messy_response)
    assert parsed["suggested_action"] == "drop"

# ==========================================
# UNIT TESTS: ACTION EXECUTION & RESILIENCE
# ==========================================

def test_fix_and_retry_safe_mutation(temp_env, mock_infrastructure):
    """Proves FixAndRetryCommand successfully injects missing fields without destroying data."""
    router = ActionRouter(mock_infrastructure["receiver"], mock_infrastructure["main_sender"], mock_infrastructure["parking_sender"])
    
    msg = MockServiceBusReceivedMessage(
        body_dict={"existing_key": "value"},
        properties={b"Resubmit-Count": 0}
    )
    
    # Execute the command mapped from dynamic regex extraction
    router.route_and_execute("fix_and_retry", msg, "missing_field_account_id")
    
    # Intercept the cloned message sent to the main queue
    sent_msg = mock_infrastructure["main_sender"].send_messages.call_args[0][0]
    payload = json.loads(b"".join(sent_msg.body).decode('utf-8'))
    
    assert payload["existing_key"] == "value"
    assert payload["account_id"] == "AUTO_FIXED_BY_AGENT"
    assert sent_msg.application_properties["AutoFixed"] == "True"

def test_broker_offline_nested_catch_safety(temp_env, mock_infrastructure):
    """Proves the nested try/catch prevents a cascading crash if ASB drops the connection."""
    router = ActionRouter(mock_infrastructure["receiver"], mock_infrastructure["main_sender"], mock_infrastructure["parking_sender"])
    
    msg = MockServiceBusReceivedMessage(body_dict={}, properties={})
    
    # Simulate Azure going completely offline during a retry attempt
    mock_infrastructure["main_sender"].send_messages.side_effect = ServiceBusError("Network partition")
    mock_infrastructure["receiver"].abandon_message.side_effect = ServiceBusError("Broker unreachable")
    
    try:
        router.route_and_execute("retry", msg, "transient_fault")
    except Exception as e:
        pytest.fail(f"Agent crashed due to unhandled nested exception: {e}")

# ==========================================
# INTEGRATION TESTS: TRIAGE PIPELINE
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
        source_queue_name="test-queue"
    )

def test_gate_a_poison_pill_quarantine(classifier, mock_infrastructure):
    """Proves Gate A intercepts infinite loops before processing logic runs."""
    msg = MockServiceBusReceivedMessage(
        body_dict={},
        properties={b"Resubmit-Count": 3}  # Exceeds default MAX_RESUBMIT_COUNT (3)
    )
    
    classifier._classify_single_message(msg)
    
    # Must immediately route to parking lot
    mock_infrastructure["parking_sender"].send_messages.assert_called_once()
    mock_infrastructure["main_sender"].send_messages.assert_not_called()

def test_gate_d_multi_tenant_overrides(classifier, mock_infrastructure):
    """Proves the heuristic engine respects tenant-specific queue overrides."""
    
    # 1. Standard Client hits the global rule (Fix and Retry)
    standard_msg = MockServiceBusReceivedMessage(
        body_dict={},
        properties={b"client_id": "Standard_Client"},
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'"
    )
    classifier._classify_single_message(standard_msg)
    mock_infrastructure["main_sender"].send_messages.assert_called_once()
    
    # 2. Special Tenant hits the override rule (Drop)
    special_msg = MockServiceBusReceivedMessage(
        body_dict={},
        properties={b"client_id": "Special_Tenant"},
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'"
    )
    classifier._classify_single_message(special_msg)
    # The drop command calls complete_message, so sender call count should NOT increment
    assert mock_infrastructure["main_sender"].send_messages.call_count == 1
    assert mock_infrastructure["receiver"].complete_message.call_count == 2

# ==========================================
# INTEGRATION TESTS: ORCHESTRATOR & MULTI-THREADING
# ==========================================

@patch("src.run_agent.shutdown_event.is_set", return_value=True) # CRITICAL FIX: Prevent infinite test hang
@patch("src.run_agent.ServiceBusClient")
@patch("src.run_agent.ThreadPoolExecutor")
def test_orchestrator_thread_allocation(mock_executor, mock_sb_client, mock_is_set, monkeypatch):
    """Proves the orchestrator maps ASB_SOURCES to isolated thread workers without cross-contamination."""
    from src.run_agent import main
    
    # Mock a multi-tenant environment with 2 queues and 1 topic
    sources = [
        {"type": "queue", "name": "orders-queue"},
        {"type": "queue", "name": "payments-queue"},
        {"type": "topic", "name": "events-topic", "subscription": "dlq-sub"}
    ]
    
    monkeypatch.setenv("ASB_SOURCES", json.dumps(sources))
    monkeypatch.setenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", "mock.servicebus.windows.net")
    
    # Intercept the ThreadPoolExecutor context manager
    mock_pool_instance = MagicMock()
    mock_executor.return_value.__enter__.return_value = mock_pool_instance
    
    # Execute the main boot sequence (will now exit immediately due to mock_is_set)
    main()
    
    # VERIFY: The orchestrator must submit exactly 3 isolated jobs to the thread pool
    assert mock_pool_instance.submit.call_count == 3
    
    # VERIFY: Ensure the correct source mappings were passed to the workers
    # (Extract the second positional argument passed to submit(), which is the source dict)
    submitted_args = [call.args[1].get("name") for call in mock_pool_instance.submit.mock_calls]
    assert "orders-queue" in submitted_args
    assert "events-topic" in submitted_args

# ==========================================
# INTEGRATION TESTS: TRIAGE PIPELINE (GATES B, C, E)
# ==========================================

def test_gate_b_idempotency_and_noise_suppression(classifier, mock_infrastructure):
    """Proves Gate B drops duplicates and suppresses noise after threshold."""
    msg = MockServiceBusReceivedMessage(
        body_dict={"id": 1},
        properties={b"client_id": "Client_A", b"Resubmit-Count": 0},
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'"
    )
    
    # 1st run: Normal processing
    classifier._classify_single_message(msg)
    contract1 = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract1["status"] == "Auto_Classified"
    
    # 2nd run: Idempotency drop
    classifier._classify_single_message(msg)
    contract2 = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract2["classification"] == "Duplicate_Transaction"
    assert contract2["status"] == "Dropped"
    
    # Run 10 more times to hit noise suppression threshold
    for _ in range(10):
        classifier._classify_single_message(msg)
        
    contract_final = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract_final["status"] == "Dropped_Threshold_Exceeded_Noise_Suppressed"
    # Ensure Parking lot is NEVER called for duplicates
    mock_infrastructure["parking_sender"].send_messages.assert_not_called()


def test_gate_c_classification_cache(classifier, mock_infrastructure):
    """Proves identical error shapes bypass the heuristics engine via cache."""
    msg1 = MockServiceBusReceivedMessage(
        body_dict={"id": 1}, # Payload 1
        properties={b"client_id": "Client_A"},
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'"
    )
    msg2 = MockServiceBusReceivedMessage(
        body_dict={"id": 2}, # Different payload hash
        properties={b"client_id": "Client_A"},
        reason="ValidationFailed", # Identical shape
        desc="missing mandatory field: 'email'"
    )
    
    classifier._classify_single_message(msg1)
    contract1 = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract1["status"] == "Auto_Classified"
    
    # Second message hits the Classification Cache
    classifier._classify_single_message(msg2)
    contract2 = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract2["status"] == "Auto_Classified_From_Cache"


def test_gate_e_ai_fallback(classifier, mock_infrastructure):
    """Proves unknown errors are routed to the AI for classification."""
    msg = MockServiceBusReceivedMessage(
        body_dict={"broken": "data"},
        properties={b"client_id": "Client_C"},
        reason="SystemFault",
        desc="Unexpected null pointer"
    )
    
    # Mock the AI returning a valid JSON string
    mock_infrastructure["ai"].call_llm.return_value = '{"suggested_classification": "AI_Classified_Fault", "suggested_pattern": "ai_found_error", "suggested_action": "escalate", "confidence_score": 0.95}'
    
    classifier._classify_single_message(msg)
    
    mock_infrastructure["ai"].call_llm.assert_called_once()
    mock_infrastructure["parking_sender"].send_messages.assert_called_once()
    
    contract = mock_infrastructure["db"].log_telemetry.call_args[0][0]
    assert contract["status"] == "AI_Suggested_Rule_Pending_Approval"
    assert contract["confidence_score"] == 0.95


# ==========================================
# UNIT TESTS: AI CLIENT NETWORK MOCK
# ==========================================

@patch("src.ai_client.requests.post")
def test_ai_client_call_llm_network_mock(mock_post, monkeypatch):
    """Proves call_llm properly formats the payload and handles successful 200 OK responses."""
    monkeypatch.setenv("OLLAMA_MODEL", "mock_model")
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://mock")
    
    client = LocalOllamaClient()
    
    # Configure the mock response
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "response": '{"suggested_classification": "Schema_Validation_Failed", "suggested_action": "fix_and_retry"}'
    }
    mock_post.return_value = mock_response
    
    # Execute network call simulation
    result = client.call_llm("Client_1", "ValidationFailed", "Missing email", '{"account": "123"}')
    
    # Verify network call was dispatched
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args[1]
    
    # Verify payload construction and parsing
    assert call_kwargs["json"]["model"] == "mock_model"
    assert "prompt" in call_kwargs["json"]
    assert result == '{"suggested_classification": "Schema_Validation_Failed", "suggested_action": "fix_and_retry"}'