import os
import json
import logging
import uuid
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("IntegrationProducer")

def main():
    fully_qualified_namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    queue_name = os.getenv("TARGET_QUEUE_NAME")

    if not fully_qualified_namespace or not queue_name:
        logger.error("Missing Service Bus configurations in .env")
        return

    credential = DefaultAzureCredential()

    # Strict test scenarios covering 100% of rules.json buckets
    test_scenarios = [
        # 1. Happy Path
        {
            "payload": {"transaction_amount": 250.00, "currency": "USD", "account": "1111"},
            "properties": {"client_id": "Alpha_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "Happy Path"
        },
        # 2. Poison Pill (Gate A) - Missing 'transaction_amount' forces it to DLQ
        {
            "payload": {"currency": "USD", "account": "2222"}, 
            "properties": {"client_id": "Beta_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 3},
            "desc": "Poison Pill (Resubmit >= 3)"
        },
        # 3. Schema Validation Failure (app_001)
        {
            "payload": {"currency": "USD", "account": "3333"},
            "properties": {"client_id": "Gamma_Inc", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "Schema Validation Failure"
        },
        # 4. Circuit Breaker Open (app_003)
        {
            "payload": {"transaction_amount": 400.00, "force_circuit_breaker": True},
            "properties": {"client_id": "Delta_LLC", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "Circuit Breaker Open"
        },
        # 5. Business Rule Violation (app_004)
        {
            "payload": {"transaction_amount": 1000.00, "client_status": "blacklisted"},
            "properties": {"client_id": "Epsilon_LLC", "message_type": "Transfer", "Resubmit-Count": 0},
            "desc": "Business Rule Violation (Blacklisted Client)"
        },
        # 6. Unknown Fault (AI Fallback / Gate E)
        {
            "payload": {"transaction_amount": 50.00, "trigger_unknown_fault": True, "broken_node": "[{}]"},
            "properties": {"client_id": "Omega_Corp", "message_type": "LegacySync", "Resubmit-Count": 0},
            "desc": "Unknown System Fault (AI Bait)"
        },
        # 7. Hard Crash Simulator (infra_002)
        {
            "payload": {"transaction_amount": 10.00, "trigger_hard_crash": True},
            "properties": {"client_id": "Zeta_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "Hard Crash Simulator (MaxDeliveryCount)"
        },
        # 8. Mock TTL Expired (infra_001)
        {
            "payload": {"transaction_amount": 10.00, "mock_infra_001": True},
            "properties": {"client_id": "Theta_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "Mock TTL Expired"
        },
        # 9. Mock Routing Loop (infra_003)
        {
            "payload": {"transaction_amount": 10.00, "mock_infra_003": True},
            "properties": {"client_id": "Iota_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "Mock Routing Loop"
        },
        # 10. Mock Capacity Exceeded (infra_004) - NEW
        {
            "payload": {"transaction_amount": 10.00, "mock_infra_004": True},
            "properties": {"client_id": "Kappa_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 0},
            "desc": "Mock Capacity Exceeded"
        }
    ]

    with ServiceBusClient(fully_qualified_namespace, credential) as client:
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

            # Malformed JSON Syntax Error (app_002)
            malformed_msg = ServiceBusMessage(
                body="{ transaction_amount: 500, broken_json_no_quotes }",
                application_properties={"client_id": "Sigma_Corp", "message_type": "PaymentRequest", "Resubmit-Count": 0},
                correlation_id=str(uuid.uuid4())
            )
            batch.append(malformed_msg)
            logger.info("Queued: Malformed JSON Syntax Error")

            # Gate B Test: Exact Duplicate
            dup_msg = ServiceBusMessage(
                body=json.dumps(test_scenarios[2]["payload"]),
                application_properties=test_scenarios[2]["properties"],
                correlation_id=batch[2].correlation_id 
            )
            batch.append(dup_msg)
            logger.info("Queued: Exact Duplicate of Schema Validation Failure (Idempotency Test)")

            sender.send_messages(batch)
            logger.info(f"Successfully dispatched {len(batch)} messages to the upstream queue.")

if __name__ == "__main__":
    main()