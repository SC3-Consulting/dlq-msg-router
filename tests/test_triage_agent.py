import pytest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from src.triage_agent import VivaDLQTriageAgent

# Mock the Azure credentials and Service Bus client so tests run offline
@pytest.fixture
@patch("src.triage_agent.DefaultAzureCredential")
@patch("src.triage_agent.ServiceBusClient")
def agent(mock_sb_client, mock_credential):
    return VivaDLQTriageAgent()

def test_fingerprint_normalization(agent):
    """
    PROVES: The system strips dynamic data (timestamps, memory addresses)
    from stack traces so identical errors generate the exact same cache hash.
    """
    client_id = "CLIENT_A"
    event = "OrderCreated"
    reason = "UnhandledException"
    
    # Trace 1: Happened at thread 1042, memory 0x1A2B
    trace_1 = "java.net.ConnectException: Connection refused thread 1042 at 0x1A2B"
    
    # Trace 2: Same error, but thread 9942, memory 0x9F4C
    trace_2 = "java.net.ConnectException: Connection refused thread 9942 at 0x9F4C"
    
    hash_1 = agent.generate_fingerprint(client_id, event, reason, trace_1)
    hash_2 = agent.generate_fingerprint(client_id, event, reason, trace_2)
    
    # The hashes MUST match despite the different numbers
    assert hash_1 == hash_2, "Paranoia Check Failed: Fingerprint normalization is allowing hash collisions!"

def test_rolling_cache_hit(agent):
    """
    PROVES: The agent respects the 10-minute token-saving deduplication window.
    """
    test_fingerprint = "abc123hash"
    
    # Inject a fake AI classification into the cache exactly 5 minutes ago
    agent.rolling_cache[test_fingerprint] = {
        "timestamp": datetime.now(timezone.utc) - timedelta(minutes=5),
        "classification": {"classification": "TestError", "action": "Retry"}
    }
    
    # Check the cache
    result = agent.check_rolling_cache(test_fingerprint)
    
    # It should return the classification because 5 mins < 10 mins
    assert result is not None
    assert result["classification"] == "TestError"

def test_rolling_cache_miss_expired(agent):
    """
    PROVES: The agent evicts cached items older than 10 minutes.
    """
    test_fingerprint = "xyz987hash"
    
    # Inject a fake AI classification from 15 minutes ago
    agent.rolling_cache[test_fingerprint] = {
        "timestamp": datetime.now(timezone.utc) - timedelta(minutes=15),
        "classification": {"classification": "ExpiredError", "action": "Drop"}
    }
    
    # Check the cache
    result = agent.check_rolling_cache(test_fingerprint)
    
    # It should return None, forcing a new LLM call
    assert result is None

def test_heuristic_routing_match(agent):
    """
    PROVES: The deterministic engine correctly identifies known errors 
    and returns the mapped action, protecting the LLM from unnecessary calls.
    """
    # 1. Inject a fake rule into the agent's memory (simulating rules.json)
    agent.rules_db = {
        "MissingTransactionDate": {
            "action": "FIX_AND_RETRY",
            "source": "heuristics"
        }
    }
    
    # 2. Simulate a message arriving with this exact reason
    reason = "MissingTransactionDate"
    dummy_fingerprint = "1a2b3c4d"
    
    # 3. Run the heuristic check
    rule = agent.check_deterministic_rules(reason, dummy_fingerprint)
    
    # 4. Paranoia Check: It MUST return the mapped action, not None.
    assert rule is not None, "Heuristic engine failed to catch a known rule!"
    assert rule["action"] == "FIX_AND_RETRY"