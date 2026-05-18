import json
import logging
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient

# Set up clean logging output
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# CONFIGURATION
FULLY_QUALIFIED_NAMESPACE = "viva-sb-ns-swastik.servicebus.windows.net"
QUEUE_NAME = "viva-integration-queue"

def process_viva_messages():
    logger.info("Initializing Consumer Zero-Trust Credential...")
    credential = DefaultAzureCredential()

    logger.info(f"Listening on Queue: {QUEUE_NAME}...")
    
    try:
        with ServiceBusClient(FULLY_QUALIFIED_NAMESPACE, credential) as client:
            # Polling configuration: stops waiting if queue is empty for 5 seconds
            with client.get_queue_receiver(queue_name=QUEUE_NAME, max_wait_time=5) as receiver:
                
                for message in receiver:
                    # Parse the incoming Canonical Data Model
                    body_str = str(message)
                    data = json.loads(body_str)
                    
                    event_id = data.get("metadata", {}).get("eventId", "UNKNOWN")
                    payload = data.get("payload", {})
                    client_id = payload.get("clientId", "UNKNOWN")
                    
                    logger.info(f"--- Processing Event ID: {event_id} ---")

                    # SCENARIO 1: The AI Catch Trigger (Simulating Catastrophic System Crash)
                    if client_id == "TRIGGER_SAP_CRASH":
                        logger.warning(f"[CRITICAL] Client ID is {client_id}. Simulating legacy SAP gateway failure...")
                        logger.error("SYSTEM ERROR: java.net.ConnectException: Connection refused (Connection timed out)")
                        
                        # Abandoning returns the message to the queue and increments delivery_count natively in Azure
                        receiver.abandon_message(message)
                        logger.warning(f"Message abandoned. Strike count incremented. (Current Delivery Count: {message.delivery_count + 1})")
                        continue

                    # SCENARIO 2: The Heuristic Catch (Deterministic Missing Business Field)
                    if "transactionDate" not in payload:
                        logger.warning("[HEURISTIC DETECTED] Missing critical business field 'transactionDate'.")
                        
                        # Explicitly dead-letter the message immediately because the error is deterministic
                        receiver.dead_letter_message(
                            message,
                            reason="MissingTransactionDate",
                            error_description="Deterministic Flaw: transactionDate is required by SAP ERP for compliance."
                        )
                        logger.info(f"Successfully dead-lettered Event {event_id} via Heuristics.")
                        continue

                    # SCENARIO 3: The Happy Path
                    logger.info(f"[SUCCESS] Order {payload.get('orderId')} for {client_id} successfully integrated into SAP.")
                    receiver.complete_message(message)

    except Exception as e:
        logger.error(f"Consumer execution halted: {str(e)}")

if __name__ == "__main__":
    process_viva_messages()