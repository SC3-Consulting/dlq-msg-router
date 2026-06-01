"""
Downstream Rejection Simulator (Consumer)

This module acts as a mock downstream application. It listens to target queues,
processes the synthetic payloads, and intentionally rejects specific payloads to 
simulate native ASB timeouts, schema errors, and infrastructure outages.
"""

import os
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from azure.servicebus import ServiceBusReceiveMode
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from src.run_agent import discover_target_queues, ServiceBusClientFactory, shutdown_event

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s')
logger = logging.getLogger("IntegrationConsumer")

def process_queue(queue_name: str, fully_qualified_namespace: str, credential: DefaultAzureCredential) -> None:
    if shutdown_event.is_set():
        return

    thread_logger = logging.getLogger(f"Consumer-{queue_name}")
    client = ServiceBusClientFactory.get_client(fully_qualified_namespace, credential)
    
    try:
        with client.get_queue_receiver(queue_name=queue_name, receive_mode=ServiceBusReceiveMode.PEEK_LOCK) as receiver:
            while not shutdown_event.is_set():
                messages = receiver.receive_messages(max_message_count=10, max_wait_time=5)
                
                if not messages:
                    break
                    
                thread_logger.info(f"Consumer processing {len(messages)} items from {queue_name}...")
                
                for message in messages:
                    try:
                        message_type = ""
                        if message.application_properties and b"message_type" in message.application_properties:
                            message_type = message.application_properties[b"message_type"].decode('utf-8')
                        elif message.application_properties and "message_type" in message.application_properties:
                            message_type = message.application_properties["message_type"]

                        try:
                            raw_payload = b"".join(message.body).decode('utf-8', errors='replace')
                            payload = json.loads(raw_payload)
                        except json.JSONDecodeError:
                            thread_logger.error(f"Rejecting {message.message_id}: Malformed JSON")
                            receiver.dead_letter_message(message, reason="MalformedMessage", error_description="Invalid JSON format.")
                            continue

                        # --- INFRASTRUCTURE FAULTS ---
                        if payload.get("mock_infra_001"):
                            receiver.dead_letter_message(message, reason="TTLExpiredException", error_description="TTL expired")
                            continue
                        if payload.get("mock_infra_003"):
                            receiver.dead_letter_message(message, reason="MaxTransferHopCountExceeded", error_description="Routing loop")
                            continue
                        if payload.get("mock_infra_004"):
                            receiver.dead_letter_message(message, reason="MessageSizeExceeded", error_description="Payload exceeds broker limits")
                            continue

                        # --- APPLICATION & SCHEMA REJECTIONS ---
                        if message_type == "PaymentRequest" and "transaction_amount" not in payload:
                            receiver.dead_letter_message(message, reason="ValidationFailed", error_description="missing mandatory field: 'transaction_amount'")
                            continue
                        # CRITICAL FIX: Target the customer_id field that exists in the safe_defaults_map
                        if message_type == "AccountSync" and "customer_id" not in payload:
                            receiver.dead_letter_message(message, reason="ValidationFailed", error_description="missing mandatory field: 'customer_id'")
                            continue
                        if payload.get("force_circuit_breaker"):
                            receiver.dead_letter_message(message, reason="CircuitBreakerOpenException", error_description="Upstream API refused")
                            continue
                        if payload.get("client_status") == "blacklisted":
                            receiver.dead_letter_message(message, reason="Business_Rule_Violation", error_description="Blacklisted client")
                            continue
                        if payload.get("trigger_unknown_fault"):
                            receiver.dead_letter_message(message, reason="SystemFault", error_description="Unexpected null pointer")
                            continue
                        if payload.get("trigger_hard_crash"):
                            raise RuntimeError("Simulated Container OOM Crash")

                        # --- HAPPY PATH ---
                        receiver.complete_message(message)

                    except Exception as e:
                        thread_logger.error(f"Consumer hard-crashed on {message.message_id}: {e}")
                        try:
                            receiver.abandon_message(message)
                        except Exception as abandon_err:
                            thread_logger.error(f"Failed to abandon message (Broker offline?): {abandon_err}")
                        continue
    except Exception as network_err:
        thread_logger.error(f"Network error attaching to {queue_name}: {network_err}")


def main() -> None:
    fully_qualified_namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    if not fully_qualified_namespace:
        logger.error("Missing SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE in .env")
        return

    credential = DefaultAzureCredential()
    
    target_queues = discover_target_queues(fully_qualified_namespace, credential)
    if not target_queues:
        logger.error("No valid target queues discovered or configured for the Consumer.")
        return

    max_workers = int(os.getenv("MAX_CONCURRENT_QUEUES", 5))
    cycle_sleep = int(os.getenv("AGENT_CYCLE_SLEEP_SECONDS", 60))
    logger.info(f"Starting simulated consumers for {len(target_queues)} queues across {max_workers} threads.")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while not shutdown_event.is_set():
            futures = {
                executor.submit(process_queue, q_name, fully_qualified_namespace, credential): q_name 
                for q_name in target_queues
            }
            
            for future in as_completed(futures):
                q_name = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.error(f"Thread processing {q_name} generated an exception: {exc}")
                    
            if not shutdown_event.is_set():
                logger.info(f"Consumer cycle complete. Sleeping for {cycle_sleep} seconds to prevent AMQP churn.")
                time.sleep(cycle_sleep)

    logger.info("Consumer shutdown complete. Closing shared AMQP connection pool...")
    client = ServiceBusClientFactory._client
    if client:
        try:
            client.close()
        except Exception as e:
            logger.error(f"Error closing ServiceBusClient: {e}")

if __name__ == "__main__":
    main()