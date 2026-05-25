import os
import logging
from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QueueFlusher")

def flush_queue(client, queue_name, is_dlq=False):
    # ASB syntax for targeting the DLQ is appending "/$DeadLetterQueue"
    target = f"{queue_name}/$DeadLetterQueue" if is_dlq else queue_name
    queue_type = "DLQ" if is_dlq else "Main Queue"
    
    logger.info(f"Connecting to {queue_type} ({target}) to flush messages...")
    
    # RECEIVE_AND_DELETE instantly destroys the message the moment it leaves the broker
    with client.get_queue_receiver(queue_name=target, receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE) as receiver:
        # max_wait_time=3 ensures the script closes automatically when the queue has been empty for 3 seconds
        messages = receiver.receive_messages(max_message_count=100, max_wait_time=3)
        
        if not messages:
            logger.info(f"[{queue_type}] is already empty.")
        else:
            logger.info(f"[{queue_type}] Flushed {len(messages)} messages into the void.")

def main():
    fully_qualified_namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    queue_name = os.getenv("TARGET_QUEUE_NAME")
    parking_lot_name = os.getenv("PARKING_LOT_QUEUE_NAME") # NEW

    if not fully_qualified_namespace or not queue_name:
        logger.error("Missing Service Bus configurations in .env")
        return

    credential = DefaultAzureCredential()

    with ServiceBusClient(fully_qualified_namespace, credential) as client:
        # 1. Flush the Main Queue
        flush_queue(client, queue_name, is_dlq=False)
        # 2. Flush the Dead Letter Queue
        flush_queue(client, queue_name, is_dlq=True)
        # 3. Flush the Parking Lot (NEW)
        flush_queue(client, parking_lot_name, is_dlq=False)
        
    logger.info("All queues are perfectly clean. Ready for testing!")

if __name__ == "__main__":
    main()