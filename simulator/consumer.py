import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("IntegrationConsumer")

def process_queue(queue_config, fully_qualified_namespace, credential):
    """Worker function to simulate downstream rejections for a specific queue."""
    queue_name = queue_config.get("name")
    if not queue_name:
        return

    thread_logger = logging.getLogger(f"Consumer-{queue_name}")
    
    with ServiceBusClient(fully_qualified_namespace, credential) as client:
        with client.get_queue_receiver(
            queue_name=queue_name, 
            receive_mode=ServiceBusReceiveMode.PEEK_LOCK
        ) as receiver:
            
            thread_logger.info(f"Consumer listening on {queue_name}...")
            
            for message in receiver:
                try:
                    # --- 1. SAFE PAYLOAD EXTRACTION ---
                    try:
                        raw_payload = b"".join(message.body).decode('utf-8')
                        payload = json.loads(raw_payload)
                    except json.JSONDecodeError:
                        thread_logger.error(f"Rejecting {message.message_id}: Malformed JSON")
                        receiver.dead_letter_message(message, reason="MalformedMessage", 
                            error_description="Invalid JSON format.")
                        continue

                    # --- 2. MOCK INFRASTRUCTURE FAULTS ---
                    if payload.get("mock_infra_001"):
                        receiver.dead_letter_message(message, reason="TTLExpiredException", error_description="TTL expired")
                        continue
                    if payload.get("mock_infra_003"):
                        receiver.dead_letter_message(message, reason="MaxTransferHopCountExceeded", error_description="Routing loop")
                        continue
                    if payload.get("mock_infra_004"):
                        receiver.dead_letter_message(message, reason="MessageSizeExceeded", error_description="Payload exceeds broker limits")
                        continue

                    # --- 3. APPLICATION & LOGIC REJECTIONS ---
                    if "transaction_amount" not in payload:
                        receiver.dead_letter_message(message, reason="ValidationFailed", error_description="missing mandatory field: 'transaction_amount'")
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

                    # --- 4. HAPPY PATH ---
                    receiver.complete_message(message)

                except Exception as e:
                    thread_logger.error(f"Consumer hard-crashed on {message.message_id}: {e}")
                    # CRITICAL FIX: Nested try/except to prevent cascading crashes if broker is offline
                    try:
                        receiver.abandon_message(message)
                    except Exception as abandon_err:
                        thread_logger.error(f"Failed to abandon message (Broker offline?): {abandon_err}")
                    continue

def main():
    fully_qualified_namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    sources_json = os.getenv("ASB_SOURCES", "[]")

    if not fully_qualified_namespace:
        logger.error("Missing SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE in .env")
        return

    try:
        target_sources = json.loads(sources_json)
        if not target_sources:
            logger.warning("No queues configured in ASB_SOURCES.")
            return
    except json.JSONDecodeError:
        logger.error("ASB_SOURCES environment variable must be a valid JSON array.")
        return

    credential = DefaultAzureCredential()

    # Launch a threaded consumer for every queue configured in the multi-tenant architecture
    with ThreadPoolExecutor(max_workers=len(target_sources)) as executor:
        for source in target_sources:
            if source.get("type") == "queue":
                executor.submit(process_queue, source, fully_qualified_namespace, credential)
            else:
                logger.warning(f"Simulator currently only supports 'queue' types. Skipping {source.get('name')}.")
                
        # Block main thread to keep executor alive
        executor.shutdown(wait=True)

if __name__ == "__main__":
    main()