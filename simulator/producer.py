"""
Synthetic Payload Generator (Producer)

This module generates and dispatches highly specific synthetic enterprise payloads 
to the Azure Service Bus. It is designed to trigger deterministic routing gates, 
validate PII masking, and exercise the AI fallback pathways for the Autonomous Agent.
"""

import os
import json
import logging
import uuid
import subprocess
import random
import string
from azure.servicebus import ServiceBusMessage
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from src.run_agent import discover_target_queues, ServiceBusClientFactory

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("IntegrationProducer")


def _resolve_service_bus_namespace() -> str:
    """Resolves Service Bus namespace from env first, then Terraform output fallback.
    Returns:
        str: Fully qualified Service Bus namespace, or empty string when unavailable.
    """
    namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", "").strip()
    if namespace and "your-namespace-here" not in namespace:
        return namespace

    try:
        terraform_output = subprocess.check_output(
            [
                "terraform",
                "-chdir=infra/terraform/azure",
                "output",
                "-raw",
                "servicebus_namespace_fqdn",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
        if terraform_output:
            logger.info(
                "Using SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE from Terraform output."
            )
            return terraform_output
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return namespace


def _run_token() -> str:
    """Creates a short per-run token so repeated producer cycles stay unique by default."""
    return uuid.uuid4().hex[:8]


def _random_suffix(length: int = 6) -> str:
    """Generates a random alphanumeric suffix of the specified length."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

def main() -> None:
    """
    Main execution script for the synthetic producer.
    Discovers target queues and dispatches batches of test anomalies.
    """
    fully_qualified_namespace = _resolve_service_bus_namespace()
    if not fully_qualified_namespace:
        logger.error("Missing SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE in environment settings")
        return

    credential = DefaultAzureCredential()
    
    target_queues = discover_target_queues(fully_qualified_namespace, credential)
    if not target_queues:
        logger.error("No valid target queues discovered or configured for the Producer.")
        return

    run_token = _run_token()
    include_duplicate_case = os.getenv("PRODUCER_INCLUDE_EXACT_DUPLICATE", "false").lower() == "true"
    include_hard_crash_case = os.getenv("PRODUCER_INCLUDE_HARD_CRASH", "false").lower() == "true"

    test_scenarios = [
        {
            "payload": {"transaction_amount": 250.00, "currency": "USD", "account": f"1111-{run_token}"},
            "properties": {"client_id": f"Alpha_Corp_{run_token}", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "1. Happy Path"
        },
        {
            "payload": {"currency": "USD", "account": f"2222-{run_token}"}, 
            "properties": {"client_id": f"Beta_Corp_{run_token}", "message_type": "PaymentRequest", "Resubmit-Count": 3},
            "desc": "2. Poison Pill (Gate A Quarantine)"
        },
        {
            "payload": {"currency": "USD", "account": f"3333-{run_token}"},
            "properties": {"client_id": f"Gamma_Inc_{run_token}", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "3. Schema Validation Failure (Float Injection)"
        },
        {
            "payload": {"transaction_amount": 400.00, "force_circuit_breaker": True, "run_token": run_token},
            "properties": {"client_id": f"Delta_LLC_{run_token}", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "4. Circuit Breaker Open"
        },
        {
            "payload": {"transaction_amount": 1000.00, "client_status": "blacklisted", "run_token": run_token},
            "properties": {"client_id": f"Epsilon_LLC_{run_token}", "message_type": "Transfer", "Resubmit-Count": 0},
            "desc": "5. Business Rule Violation (Blacklisted Client)"
        },
        {
            "payload": {
                "transaction_amount": 50.00, 
                "trigger_unknown_fault": True,
                "customer_email": "test.user@financial.com",
                "customer_phone": "+1234567890",
                "credit_card": "4111 1111 1111 1111",
                "run_token": run_token
            },
            "properties": {"client_id": f"Omega_Corp_{run_token}", "message_type": "LegacySync", "Resubmit-Count": 0},
            "desc": "6. Unknown System Fault (AI Fallback + PII Scrubber)"
        },
        {
            "payload": {"transaction_amount": 10.00, "mock_infra_004": True, "run_token": run_token},
            "properties": {"client_id": f"Kappa_Corp_{run_token}", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "8. Mock Capacity Exceeded"
        },
        {
            "payload": {"currency": "GBP", "account": f"9999-{run_token}", "trigger_cache_test": True},
            "properties": {"client_id": f"Gamma_Inc_{run_token}", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "9. Cache Hit Verification (Gate C Bypass)"
        },
        {
            # CRITICAL FIX: Tests 'customer_id' string injection which actually exists in rules.json
            "payload": {"plan": "premium", "trigger_string_inject": True, "run_token": run_token}, 
            "properties": {"client_id": f"Theta_Corp_{run_token}", "message_type": "AccountSync", "Resubmit-Count": 0},
            "desc": "10. Dynamic Safe Default Test (String Injection)"
        }
    ]

    if include_hard_crash_case:
        test_scenarios.insert(
            6,
            {
                "payload": {"transaction_amount": 10.00, "trigger_hard_crash": True, "run_token": run_token},
                "properties": {"client_id": f"Zeta_Corp_{run_token}", "message_type": "PaymentRequest", "Resubmit-Count": 0},
                "desc": "7. Hard Crash Simulator (Nested Exception Check)"
            },
        )

    client = ServiceBusClientFactory.get_client(fully_qualified_namespace, credential)
    
    try:
        for queue_name in target_queues:
            with client.get_queue_sender(queue_name=queue_name) as sender:
                logger.info(f"Producer connected to {queue_name}. Building synthetic batch...")

                batch = []
                for test in test_scenarios:
                    msg = ServiceBusMessage(
                        body=json.dumps(test["payload"]),
                        application_properties=test["properties"],
                        correlation_id=str(uuid.uuid4())
                    )
                    batch.append(msg)
                    logger.info(f"Queued: {test['desc']}")

                malformed_msg = ServiceBusMessage(
                    body="{ transaction_amount: 500, broken_json_no_quotes }",
                    application_properties={"client_id": f"Sigma_Corp_{run_token}", "message_type": "PaymentRequest", "Resubmit-Count": 0},
                    correlation_id=str(uuid.uuid4())
                )
                batch.append(malformed_msg)
                logger.info("Queued: 11. Malformed JSON Syntax Error")

                if include_duplicate_case:
                    dup_msg = ServiceBusMessage(
                        body=json.dumps(test_scenarios[2]["payload"]),
                        application_properties=test_scenarios[2]["properties"],
                        correlation_id=batch[2].correlation_id 
                    )
                    batch.append(dup_msg)
                    logger.info("Queued: 12. Exact Duplicate (Gate B Idempotency Test)")
                else:
                    logger.info("Skipping exact duplicate case unless PRODUCER_INCLUDE_EXACT_DUPLICATE=true")

                sender.send_messages(batch)
                logger.info(f"Successfully dispatched {len(batch)} messages to {queue_name}.")
    finally:
        logger.info("Producer dispatch complete. Closing shared AMQP connection pool...")
        client.close()

if __name__ == "__main__":
    main()