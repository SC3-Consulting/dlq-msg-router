import os
import logging
import csv
from datetime import datetime
from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from src.autonomous_dlq_classifier import AutonomousDLQClassifier
from src.InMemoryCache import InMemoryCache
from src.ai_client import LocalOllamaClient

# A simple mock database to print the JSON contracts to the terminal during the demo
# A mock database to print to the terminal AND generate a CSV dashboard for MVP
class DemoTerminalDatabase:
    def __init__(self, filepath="reports/telemetry_dashboard.csv"):
        self.filepath = filepath
        self.headers = [
            "timestamp", "source_queue", "client_id", "message_type", 
            "classification", "pattern", "status", "occurrence_count", 
            "suggested_action", "confidence_score"
        ]
        # --- NEW: Ensure the directory exists before creating the file ---
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        # Initialize the CSV with headers if it doesn't exist yet
        if not os.path.exists(self.filepath):
            with open(self.filepath, mode='w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(self.headers) 

    def log_telemetry(self, contract):
        # 1. Print to terminal (Existing behavior for real-time tracking)
        print(f"\n[{contract['status'].upper()}] - {contract['classification']}")
        print(f" -> Pattern: {contract['pattern']}")
        if 'suggested_action' in contract:
            print(f" -> Action:  {contract['suggested_action']}")
        print("-" * 50)

        # 2. Append to CSV Dashboard (New behavior for Andy's requirement)
        row = [
            datetime.now().isoformat(),
            contract.get("source_queue", ""),
            contract.get("client_id", ""),
            contract.get("message_type", ""),
            contract.get("classification", ""),
            contract.get("pattern", ""),
            contract.get("status", ""),
            contract.get("occurrence_count", 1),
            contract.get("suggested_action", "N/A"),
            contract.get("confidence_score", "N/A")
        ]
        
        with open(self.filepath, mode='a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)

def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger("AgentRunner")

    fully_qualified_namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    queue_name = os.getenv("TARGET_QUEUE_NAME")
    
    if not fully_qualified_namespace or not queue_name:
        logger.error("Missing Service Bus configurations in .env")
        return

    # ASB syntax for the DLQ is the main queue name appended with /$DeadLetterQueue
    dlq_name = f"{queue_name}/$DeadLetterQueue"
    parking_lot_name = os.getenv("PARKING_LOT_QUEUE_NAME", "parking-lot-queue")

    credential = DefaultAzureCredential()
    
    with ServiceBusClient(fully_qualified_namespace, credential) as client:
        with client.get_queue_receiver(queue_name=dlq_name, receive_mode=ServiceBusReceiveMode.PEEK_LOCK) as dlq_receiver, \
             client.get_queue_sender(queue_name=parking_lot_name) as parking_lot_sender:
            
            logger.info("Booting Autonomous DLQ Agent...")
            agent = AutonomousDLQClassifier(
                idempotency_cache=InMemoryCache(default_ttl_seconds=86400),
                classification_cache=InMemoryCache(default_ttl_seconds=600),
                ai_client=LocalOllamaClient(),
                database_client=DemoTerminalDatabase(),
                parking_lot_sender=parking_lot_sender,
                dlq_receiver=dlq_receiver,
                source_queue_name=queue_name
            )
            
            logger.info(f"Agent actively polling {dlq_name} for anomalies...")
            
            # The Infinite Polling Loop
            while True:
                messages = dlq_receiver.receive_messages(max_message_count=10, max_wait_time=5)
                if messages:
                    logger.info(f"Found {len(messages)} messages in DLQ. Handing to Classifier...")
                    agent.process_batch(messages)

if __name__ == "__main__":
    main()