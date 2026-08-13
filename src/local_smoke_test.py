"""
This module implements a local smoke test for the DLQ Smart Router agent.
- It sends a test message to a specified Azure Service Bus queue, then attempts to receive and dead-letter it, simulating a validation failure scenario.
- The agent is expected to process the dead-lettered message and increment the messages_processed_total metric.
- The test verifies that the agent is running, the Service Bus emulator is healthy, and that the agent successfully processes the injected DLQ message within a specified timeout.
- The test can be configured via environment variables for queue names, health endpoints, and timeouts, allowing for flexible integration testing in different environments.
"""

import json
import os
import sys
import time
import urllib.request
import uuid

from azure.servicebus import (
    ServiceBusClient,
    ServiceBusMessage,
    ServiceBusReceiveMode,
)


def _get_json(url):
    """
    Fetches JSON data from the specified URL and returns it as a Python dictionary.
    Args:
        url (str): The URL to fetch JSON data from.
    Returns:
        dict: The JSON data as a Python dictionary.
    """
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_url(url, timeout_seconds):
    """
    Waits for the specified URL to become available and return a successful HTTP response within the given timeout.
    Args:
        url (str): The URL to check for availability.
        timeout_seconds (int): The maximum time to wait for the URL to become available, in seconds.
    Returns:
        bool: True if the URL became available within the timeout, False otherwise.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            _get_json(url)
            return True
        except Exception:
            time.sleep(1)
    return False


def _extract_property(message, key):
    """
    Extracts the value of a specified property from a Service Bus message.
    Args:
        message (ServiceBusReceivedMessage): The message to extract the property from.
        key (str): The property key to look for.
    Returns:
        str: The value of the property, or None if not found.
    """
    props = message.application_properties or {}
    for prop_key, value in props.items():
        normalised = (
            prop_key.decode("utf-8") if isinstance(prop_key, bytes) else str(prop_key)
        )
        if normalised == key:
            return value.decode("utf-8") if isinstance(value, bytes) else str(value)
    return None


def main():
    """
    Main function to perform the local smoke test for the DLQ Smart Router agent.
    It checks for required environment variables, sends a test message to the specified queue,
    and verifies that the message is processed correctly.
    Returns:
        int: Exit code indicating success (0) or failure (1).
    """
    conn_str = os.getenv("SERVICE_BUS_CONNECTION_STRING")
    if not conn_str:
        print("[smoke] ERROR: SERVICE_BUS_CONNECTION_STRING is required.")
        return 1

    queue_name = os.getenv("SMOKE_TEST_QUEUE", "integration-queue")
    metrics_url = os.getenv("SMOKE_TEST_METRICS_URL", "http://127.0.0.1:8080/metrics")
    agent_health_url = os.getenv(
        "SMOKE_TEST_AGENT_HEALTH_URL", "http://127.0.0.1:8080/health"
    )
    emulator_health_url = os.getenv(
        "SMOKE_TEST_EMULATOR_HEALTH_URL", "http://servicebus-emulator:5300/health"
    )
    startup_timeout = int(os.getenv("SMOKE_TEST_STARTUP_TIMEOUT_SECONDS", "120"))
    processing_timeout = int(os.getenv("SMOKE_TEST_PROCESSING_TIMEOUT_SECONDS", "120"))

    if not _wait_for_url(agent_health_url, startup_timeout):
        print(f"[smoke] ERROR: agent health endpoint not ready: {agent_health_url}")
        return 1

    if not _wait_for_url(emulator_health_url, startup_timeout):
        print(
            f"[smoke] ERROR: emulator health endpoint not ready: {emulator_health_url}"
        )
        return 1

    baseline_metrics = _get_json(metrics_url)
    baseline_processed = int(
        baseline_metrics.get("counters", {}).get("messages_processed_total", 0)
    )

    smoke_id = str(uuid.uuid4())
    payload = {
        "client_id": "Smoke_Test_Client",
        "message_type": "SmokeTest",
        "reason": "ValidationFailed",
        "smoke_test_id": smoke_id,
    }

    with ServiceBusClient.from_connection_string(conn_str) as sb_client:
        with sb_client.get_queue_sender(queue_name=queue_name) as sender:
            sender.send_messages(
                ServiceBusMessage(
                    json.dumps(payload),
                    application_properties={"smoke_test_id": smoke_id},
                    correlation_id=smoke_id,
                )
            )

        found = False
        with sb_client.get_queue_receiver(
            queue_name=queue_name,
            receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
            max_wait_time=10,
        ) as receiver:
            messages = receiver.receive_messages(max_message_count=20, max_wait_time=10)
            for message in messages:
                message_smoke_id = _extract_property(message, "smoke_test_id")
                if message_smoke_id == smoke_id:
                    # Use a deterministic heuristic-matching reason/description so smoke runs
                    # do not trigger LLM fallback and heavy local model inference.
                    receiver.dead_letter_message(
                        message,
                        reason="ValidationFailed",
                        error_description=(
                            f"smoke_test_id={smoke_id}; missing mandatory field: 'transaction_amount'"
                        ),
                    )
                    found = True
                else:
                    receiver.abandon_message(message)

        if not found:
            print(
                "[smoke] ERROR: could not receive sent smoke message for dead-letter injection."
            )
            return 1

    deadline = time.time() + processing_timeout
    while time.time() < deadline:
        metrics = _get_json(metrics_url)
        processed_total = int(
            metrics.get("counters", {}).get("messages_processed_total", 0)
        )
        if processed_total > baseline_processed:
            print(
                f"[smoke] PASS: messages_processed_total increased from {baseline_processed} to {processed_total}."
            )
            return 0
        time.sleep(2)

    final_metrics = _get_json(metrics_url)
    failures_total = int(final_metrics.get("counters", {}).get("failures_total", 0))
    print(
        "[smoke] ERROR: agent did not process the injected DLQ message within timeout. "
        f"messages_processed_total stayed at {baseline_processed}, failures_total={failures_total}."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
