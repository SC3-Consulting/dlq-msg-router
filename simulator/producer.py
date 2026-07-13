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
from azure.servicebus import ServiceBusMessage
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from src.run_agent import discover_target_queues, ServiceBusClientFactory

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("IntegrationProducer")


def _resolve_service_bus_namespace() -> str:
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

    test_scenarios = [
        {
            "payload": {"transaction_amount": 250.00, "currency": "USD", "account": "1111"},
            "properties": {"client_id": "Alpha_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "1. Happy Path"
        },
        {
            "payload": {"currency": "USD", "account": "2222"}, 
            "properties": {"client_id": "Beta_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 3},
            "desc": "2. Poison Pill (Gate A Quarantine)"
        },
        {
            "payload": {"currency": "USD", "account": "3333"},
            "properties": {"client_id": "Gamma_Inc", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "3. Schema Validation Failure (Float Injection)"
        },
        {
            "payload": {"transaction_amount": 400.00, "force_circuit_breaker": True},
            "properties": {"client_id": "Delta_LLC", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "4. Circuit Breaker Open"
        },
        {
            "payload": {"transaction_amount": 1000.00, "client_status": "blacklisted"},
            "properties": {"client_id": "Epsilon_LLC", "message_type": "Transfer", "Resubmit-Count": 0},
            "desc": "5. Business Rule Violation (Blacklisted Client)"
        },
        {
            "payload": {
                "transaction_amount": 50.00, 
                "trigger_unknown_fault": True,
                "customer_email": "test.user@financial.com",
                "customer_phone": "+1234567890",
                "credit_card": "4111 1111 1111 1111" 
            },
            "properties": {"client_id": "Omega_Corp", "message_type": "LegacySync", "Resubmit-Count": 0},
            "desc": "6. Unknown System Fault (AI Fallback + PII Scrubber)"
        },
        {
            "payload": {"transaction_amount": 10.00, "trigger_hard_crash": True},
            "properties": {"client_id": "Zeta_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "7. Hard Crash Simulator (Nested Exception Check)"
        },
        {
            "payload": {"transaction_amount": 10.00, "mock_infra_004": True},
            "properties": {"client_id": "Kappa_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "8. Mock Capacity Exceeded"
        },
        {
            "payload": {"currency": "GBP", "account": "9999", "trigger_cache_test": True},
            "properties": {"client_id": "Gamma_Inc", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "9. Cache Hit Verification (Gate C Bypass)"
        },
        {
            # CRITICAL FIX: Tests 'customer_id' string injection which actually exists in rules.json
            "payload": {"plan": "premium", "trigger_string_inject": True}, 
            "properties": {"client_id": "Theta_Corp", "message_type": "AccountSync", "Resubmit-Count": 0},
            "desc": "10. Dynamic Safe Default Test (String Injection)"
        }
    ]

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
                    application_properties={"client_id": "Sigma_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 0},
                    correlation_id=str(uuid.uuid4())
                )
                batch.append(malformed_msg)
                logger.info("Queued: 11. Malformed JSON Syntax Error")

                dup_msg = ServiceBusMessage(
                    body=json.dumps(test_scenarios[2]["payload"]),
                    application_properties=test_scenarios[2]["properties"],
                    correlation_id=batch[2].correlation_id 
                )
                batch.append(dup_msg)
                logger.info("Queued: 12. Exact Duplicate (Gate B Idempotency Test)")

                sender.send_messages(batch)
                logger.info(f"Successfully dispatched {len(batch)} messages to {queue_name}.")
    finally:
        logger.info("Producer dispatch complete. Closing shared AMQP connection pool...")
        client.close()

if __name__ == "__main__":
    main()