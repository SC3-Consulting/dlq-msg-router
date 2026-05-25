import os
import json
import logging
from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("IntegrationConsumer")

def main():
    fully_qualified_namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    queue_name = os.getenv("TARGET_QUEUE_NAME")

    if not fully_qualified_namespace or not queue_name:
        logger.error("Missing Service Bus configurations in .env")
        return

    credential = DefaultAzureCredential()
    
    with ServiceBusClient(fully_qualified_namespace, credential) as client:
        with client.get_queue_receiver(
            queue_name=queue_name, 
            receive_mode=ServiceBusReceiveMode.PEEK_LOCK
        ) as receiver:
            
            logger.info(f"Consumer listening on {queue_name}...")
            
            for message in receiver:
                try:
                    # --- 1. SAFE PAYLOAD EXTRACTION ---
                    try:
                        raw_payload = b"".join(message.body).decode('utf-8')
                        payload = json.loads(raw_payload)
                    except json.JSONDecodeError:
                        logger.error(f"Rejecting {message.message_id}: Malformed JSON")
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
                    if payload.get("mock_infra_004"): # NEW: Tests infra_004 rule
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
                    logger.error(f"Consumer hard-crashed on {message.message_id}: {e}")
                    receiver.abandon_message(message)
                    continue

if __name__ == "__main__":
    main()