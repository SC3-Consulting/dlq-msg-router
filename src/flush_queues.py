import os
import json
import logging
from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QueueFlusher")

def flush_queue(client, queue_name, is_dlq=False):
    target = f"{queue_name}/$DeadLetterQueue" if is_dlq else queue_name
    queue_type = "DLQ" if is_dlq else "Main Queue"
    
    logger.info(f"Connecting to {queue_type} ({target}) to flush messages...")
    
    with client.get_queue_receiver(queue_name=target, receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE) as receiver:
        messages = receiver.receive_messages(max_message_count=100, max_wait_time=3)
        
        if not messages:
            logger.info(f"[{queue_type}] is already empty.")
        else:
            logger.info(f"[{queue_type}] Flushed {len(messages)} messages into the void.")

def main():
    fully_qualified_namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    sources_json = os.getenv("ASB_SOURCES", "[{\"type\": \"queue\",\"name\": \"viva-payments-queue\"},{\"type\": \"queue\",\"name\": \"viva-integration-queue\"}]")
    parking_lot_name = os.getenv("PARKING_LOT_QUEUE_NAME")

    if not fully_qualified_namespace:
        logger.error("Missing SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE in .env")
        return

    try:
        target_sources = json.loads(sources_json)
    except json.JSONDecodeError:
        logger.error("ASB_SOURCES must be a valid JSON array.")
        return

    conn_str = os.getenv("SERVICE_BUS_CONNECTION_STRING")
    
    if conn_str:
        client = ServiceBusClient.from_connection_string(conn_str)
    else:
        credential = DefaultAzureCredential()
        client = ServiceBusClient(fully_qualified_namespace, credential)

    with client:
        # 1. Flush all configured tenant queues (Main + DLQ)
        for source in target_sources:
            if source.get("type") == "queue":
                queue_name = source.get("name")
                logger.info(f"--- Sweeping {queue_name} ---")
                flush_queue(client, queue_name, is_dlq=False)
                flush_queue(client, queue_name, is_dlq=True)
                
        # 2. Flush the Parking Lot
        if parking_lot_name:
            logger.info("--- Sweeping Parking Lot ---")
            flush_queue(client, parking_lot_name, is_dlq=False)
            
    logger.info("All queues are perfectly clean. Ready for testing!")

if __name__ == "__main__":
    main()