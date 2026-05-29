import os
import json
import csv
import time
import signal
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode, ServiceBusSubQueue
from azure.identity import DefaultAzureCredential

from src.autonomous_dlq_classifier import AutonomousDLQClassifier
from src.state_managers import IdempotencyStore, ClassificationCache
from src.ai_client import LocalOllamaClient

# Global event to signal all threads to shut down gracefully
shutdown_event = threading.Event()

def signal_handler(signum, frame):
    logger = logging.getLogger("AgentRunner")
    logger.warning("\n[SIGINT] Termination signal detected. Initiating graceful shutdown...")
    logger.warning("Please wait for threads to finish their current ASB batch to prevent orphaned locks.")
    shutdown_event.set()

signal.signal(signal.SIGINT, signal_handler)

class DemoTerminalDatabase:
    """Mock database to print terminal output AND generate a CSV dashboard for MVP."""
    def __init__(self, filepath=None):
        # Parameterise telemetry path
        self.filepath = filepath or os.getenv("TELEMETRY_CSV_PATH", "reports/telemetry_dashboard.csv")
        self.headers = [
            "timestamp", "source_queue", "client_id", "message_type", 
            "classification", "pattern", "status", "occurrence_count", 
            "suggested_action", "confidence_score"
        ]
        self.lock = threading.Lock() # Ensure thread-safe CSV writes
        
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        if not os.path.exists(self.filepath):
            with open(self.filepath, mode='w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(self.headers) 

    def log_telemetry(self, contract):
        with self.lock:
            print(f"\n[{contract['status'].upper()}] - {contract['classification']}")
            print(f" -> Pattern: {contract['pattern']}")
            if 'suggested_action' in contract:
                print(f" -> Action:  {contract['suggested_action']}")
            print("-" * 50)

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

def disk_cleanup_daemon(store: IdempotencyStore):
    """Background thread that runs hourly to purge expired dbm keys."""
    while not shutdown_event.is_set():
        # Sleep for 1 hour, broken into small checks to allow graceful shutdown
        for _ in range(3600):
            if shutdown_event.is_set():
                return
            time.sleep(1)
        store.cleanup_expired()

def process_dlq_source(source_config, idempotency_store, classification_cache, ai_client, db_client, fully_qualified_namespace, credential):
    """Worker function executed per thread to monitor a specific Queue or Topic."""
    source_type = source_config.get("type", "").lower()
    source_name = source_config.get("name")
    parking_lot_name = os.getenv("PARKING_LOT_QUEUE_NAME", "parking-lot-queue")
    
    # Parameterise polling values from .env with fallbacks
    max_msg_count = int(os.getenv("ASB_MAX_MESSAGE_COUNT", 10))
    max_wait_time = int(os.getenv("ASB_MAX_WAIT_TIME", 5))
    
    logger = logging.getLogger(f"Worker-{source_name}")
    logger.info(f"Initialising thread for {source_type}: {source_name}")
    
    # CRITICAL: Isolated AMQP Client per thread to prevent cross-thread network collisions
    with ServiceBusClient(fully_qualified_namespace, credential) as client:
        try:
            if source_type == "queue":
                dlq_receiver = client.get_queue_receiver(
                    queue_name=source_name, 
                    sub_queue=ServiceBusSubQueue.DEAD_LETTER, 
                    receive_mode=ServiceBusReceiveMode.PEEK_LOCK
                )
                main_sender = client.get_queue_sender(queue_name=source_name)
                
            elif source_type == "topic":
                subscription = source_config.get("subscription")
                if not subscription:
                    raise ValueError(f"Topic '{source_name}' requires a 'subscription' configuration.")
                    
                dlq_receiver = client.get_subscription_receiver(
                    topic_name=source_name, 
                    subscription_name=subscription, 
                    sub_queue=ServiceBusSubQueue.DEAD_LETTER, 
                    receive_mode=ServiceBusReceiveMode.PEEK_LOCK
                )
                # Topic retries are sent to the topic itself, not the subscription
                main_sender = client.get_topic_sender(topic_name=source_name)
                
            else:
                logger.error(f"Unrecognised source type '{source_type}'. Skipping.")
                return
                
            parking_lot_sender = client.get_queue_sender(queue_name=parking_lot_name)
            
            agent = AutonomousDLQClassifier(
                idempotency_cache=idempotency_store,
                classification_cache=classification_cache,
                ai_client=ai_client,
                database_client=db_client,
                parking_lot_sender=parking_lot_sender,
                main_queue_sender=main_sender,
                dlq_receiver=dlq_receiver,
                source_queue_name=source_name
            )
            
            logger.info(f"Actively polling DLQ for {source_name}...")
            
            while not shutdown_event.is_set():
                # Short max_wait_time allows the loop to check shutdown_event frequently
                messages = dlq_receiver.receive_messages(
                    max_message_count=max_msg_count, 
                    max_wait_time=max_wait_time
                )
                if messages:
                    logger.info(f"Found {len(messages)} anomalies in {source_name}. Processing...")
                    agent.process_batch(messages)
                    
        except Exception as e:
            logger.error(f"Thread for {source_name} crashed: {e}")
        finally:
            logger.info(f"Thread for {source_name} shutting down safely.")

def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger("AgentRunner")

    fully_qualified_namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    sources_json = os.getenv("ASB_SOURCES", "[]")
    
    if not fully_qualified_namespace:
        logger.error("Missing SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE in .env")
        return

    try:
        target_sources = json.loads(sources_json)
        if not target_sources:
            logger.warning("No queues/topics configured in ASB_SOURCES. Agent will exit.")
            return
    except json.JSONDecodeError:
        logger.error("ASB_SOURCES environment variable must be a valid JSON array.")
        return

    credential = DefaultAzureCredential()
    
    # Shared state and clients
    idempotency_store = IdempotencyStore()
    classification_cache = ClassificationCache()
    ai_client = LocalOllamaClient()
    db_client = DemoTerminalDatabase()

    # Spin up the disk cleanup daemon
    cleanup_thread = threading.Thread(target=disk_cleanup_daemon, args=(idempotency_store,), daemon=True)
    cleanup_thread.start()

    logger.info(f"Booting Autonomous DLQ Agent with {len(target_sources)} target sources...")

    with ThreadPoolExecutor(max_workers=len(target_sources)) as executor:
        for source in target_sources:
            executor.submit(
                process_dlq_source, 
                source, 
                idempotency_store, 
                classification_cache, 
                ai_client, 
                db_client, 
                fully_qualified_namespace, 
                credential
            )
            
        # Keep the main thread alive to catch SIGINT
        while not shutdown_event.is_set():
            time.sleep(1)
            
    logger.info("Agent shutdown complete. All connections closed.")

if __name__ == "__main__":
    main()