import pytest
import json
import hashlib
from unittest.mock import MagicMock
from src.autonomous_dlq_classifier import AutonomousDLQClassifier
from src.InMemoryCache import InMemoryCache

# --- MOCK AZURE MESSAGE ---
class MockServiceBusMessage:
    def __init__(self, body_dict, application_properties, reason=None, desc=None, msg_id="123", corr_id="abc"):
        self.body = [json.dumps(body_dict).encode('utf-8')]
        self.application_properties = application_properties
        self.message_id = msg_id
        self.correlation_id = corr_id
        self.dead_letter_reason = reason
        self.dead_letter_error_description = desc
        self.subject = "TestSubject"
        self.content_type = "application/json"

# --- PYTEST FIXTURES ---
@pytest.fixture
def mock_db():
    return MagicMock()

@pytest.fixture
def mock_parking_lot():
    return MagicMock()

@pytest.fixture
def mock_dlq_receiver():
    return MagicMock()

@pytest.fixture
def mock_ai_client():
    ai = MagicMock()
    # Default AI response
    ai.call_llm.return_value = """
    ```json
    {
        "suggested_classification": "AI_Classified_Fault",
        "suggested_pattern": "ai_found_error",
        "suggested_action": "escalate",
        "detection_rule": "$.error",
        "confidence_score": 0.95,
        "reasoning_summary": "Test reasoning"
    }
    ```
    """
    return ai

@pytest.fixture
def classifier(mock_db, mock_parking_lot, mock_dlq_receiver, mock_ai_client, monkeypatch):
    # Mock the rules path to ensure it finds your rules.json during testing
    monkeypatch.setenv("RULES_FILE_PATH", "data/rules.json") 
    
    return AutonomousDLQClassifier(
        idempotency_cache=InMemoryCache(default_ttl_seconds=86400),
        classification_cache=InMemoryCache(default_ttl_seconds=600),
        ai_client=mock_ai_client,
        database_client=mock_db,
        parking_lot_sender=mock_parking_lot,
        dlq_receiver=mock_dlq_receiver,
        source_queue_name="test-dlq"
    )

# --- TEST CASES ---

def test_gate_a_poison_pill(classifier, mock_parking_lot, mock_dlq_receiver):
    """Test that a message with 3 resubmits is quarantined to the parking lot."""
    msg = MockServiceBusMessage(
        body_dict={"test": "data"},
        application_properties={"client_id": "Client_A", "Resubmit-Count": 3}
    )
    
    classifier._classify_single_message(msg)
    
    # Assert it routed to parking lot and completed the DLQ message
    assert mock_parking_lot.send_messages.called
    assert mock_dlq_receiver.complete_message.called
    
    # Verify telemetry contract
    contract = classifier.db.log_telemetry.call_args[0][0]
    assert contract["status"] == "Quarantined"
    assert contract["classification"] == "Resubmit_Limit_Exhausted"

def test_gate_b_idempotency_and_noise_suppression(classifier, mock_parking_lot):
    """Test that duplicates are dropped, and >10 duplicates suppresses noise."""
    msg = MockServiceBusMessage(
        body_dict={"id": 1},
        application_properties={"client_id": "Client_A", "Resubmit-Count": 0},
        reason="ValidationFailed",                     # FIX: Added a known reason
        desc="missing mandatory field: 'email'"        # FIX: Added a known description
    )
    
    # Send 1st time (Should process normally via Gate D)
    classifier._classify_single_message(msg)
    contract1 = classifier.db.log_telemetry.call_args[0][0]
    assert contract1["status"] == "Auto_Classified"
    
    # Send 2nd time (Should hit Idempotency cache and Drop)
    classifier._classify_single_message(msg)
    contract2 = classifier.db.log_telemetry.call_args[0][0]
    assert contract2["classification"] == "Duplicate_Transaction"
    assert contract2["status"] == "Dropped"
    assert contract2["occurrence_count"] == 2
    
    # Send 10 more times to trigger noise suppression threshold
    for _ in range(10):
        classifier._classify_single_message(msg)
        
    contract_final = classifier.db.log_telemetry.call_args[0][0]
    assert contract_final["status"] == "Dropped_Threshold_Exceeded_Noise_Suppressed"
    assert contract_final["occurrence_count"] == 12
    # Ensure Parking lot is NEVER called for duplicates
    assert not mock_parking_lot.send_messages.called

def test_gate_c_classification_cache(classifier):
    """Test that semantic error shapes hit the cache, saving CPU cycles."""
    msg1 = MockServiceBusMessage(
        body_dict={"id": 1}, # Payload 1
        application_properties={"client_id": "Client_A"},
        reason="ValidationFailed",
        desc="missing mandatory field: 'email'"
    )
    msg2 = MockServiceBusMessage(
        body_dict={"id": 2}, # Payload 2 (Different hash)
        application_properties={"client_id": "Client_A"},
        reason="ValidationFailed", # Identical shape
        desc="missing mandatory field: 'email'"
    )
    
    # First message processes via rules engine
    classifier._classify_single_message(msg1)
    contract1 = classifier.db.log_telemetry.call_args[0][0]
    assert contract1["status"] == "Auto_Classified"
    
    # Second message hits the Classification Cache
    classifier._classify_single_message(msg2)
    contract2 = classifier.db.log_telemetry.call_args[0][0]
    assert contract2["status"] == "Auto_Classified_From_Cache"
    assert contract2["pattern"] == "missing_field_email"

def test_gate_d_heuristic_regex_routing(classifier):
    """Test that deterministic regex rules execute correctly and map actions."""
    msg = MockServiceBusMessage(
        body_dict={"test": "data"},
        application_properties={"client_id": "Client_B"},
        reason="ValidationFailed",
        desc="missing mandatory field: 'transaction_amount'"
    )
    
    classifier._classify_single_message(msg)
    
    contract = classifier.db.log_telemetry.call_args[0][0]
    assert contract["classification"] == "Schema_Validation_Failed"
    assert contract["pattern"] == "missing_field_transaction_amount"
    assert contract["suggested_action"] == "fix_and_retry"

def test_gate_e_ai_fallback_high_confidence(classifier, mock_ai_client, mock_parking_lot):
    """Test AI is invoked for Unknowns, and high confidence flags for rule approval."""
    msg = MockServiceBusMessage(
        body_dict={"broken": "data"},
        application_properties={"client_id": "Client_C"},
        reason="SystemFault",
        desc="Unexpected null pointer"
    )
    
    classifier._classify_single_message(msg)
    
    assert mock_ai_client.call_llm.called
    assert mock_parking_lot.send_messages.called
    
    contract = classifier.db.log_telemetry.call_args[0][0]
    assert contract["status"] == "AI_Suggested_Rule_Pending_Approval"
    assert contract["confidence_score"] == 0.95

def test_gate_e_ai_fallback_low_confidence(classifier, mock_ai_client):
    """Test AI low confidence flags for manual review."""
    # Alter the mock to return low confidence
    mock_ai_client.call_llm.return_value = '{"suggested_classification": "Unknown", "confidence_score": 0.40}'
    
    msg = MockServiceBusMessage(
        body_dict={"broken": "data"},
        application_properties={"client_id": "Client_C"},
        reason="SystemFault",
        desc="Unexpected null pointer"
    )
    
    classifier._classify_single_message(msg)
    contract = classifier.db.log_telemetry.call_args[0][0]
    assert contract["status"] == "AI_Low_Confidence_Manual_Review"

def test_json_salvage_robustness(classifier):
    """Test the regex parser correctly extracts JSON from conversational AI output."""
    messy_llm_output = """
    Here is my analysis of the Dead Letter Queue message:
    ```json
    {
        "suggested_classification": "Network_Error",
        "suggested_pattern": "timeout_occurred",
        "confidence_score": 0.88
    }
    ```
    Please let me know if you need anything else.
    """
    
    parsed_json = classifier._salvage_json(messy_llm_output)
    assert parsed_json["suggested_classification"] == "Network_Error"
    assert parsed_json["confidence_score"] == 0.88