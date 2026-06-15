import os
import json
import csv
import time
import signal
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode, ServiceBusSubQueue
from azure.servicebus.management import ServiceBusAdministrationClient
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential

from src.autonomous_dlq_classifier import AutonomousDLQClassifier
from src.state_managers import IdempotencyStore, ClassificationCache
from src.ai_client import AIEngineFactory

# Configure enterprise logging standard
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s'
)
logger = logging.getLogger("AgentOrchestrator")

# Global event to signal all threads to shut down gracefully
shutdown_event = threading.Event()

def signal_handler(signum, frame):
    logger.warning("\n[SIGINT] Termination signal detected. Initiating graceful shutdown...")
    logger.warning("Please wait for threads to finish their current ASB batch to prevent orphaned locks.")
    shutdown_event.set()

signal.signal(signal.SIGINT, signal_handler)


class DemoTerminalDatabase:
    """Mock database to print terminal output AND generate a CSV dashboard for MVP."""
    def __init__(self, filepath=None):
        self.filepath = filepath or os.getenv("TELEMETRY_CSV_PATH", "reports/telemetry_dashboard.csv")
        self.headers = [
            "timestamp", "source_queue", "client_id", "message_type", 
            "classification", "pattern", "status", "occurrence_count", 
            "suggested_action", "confidence_score"
        ]
        self.lock = threading.Lock() 
        
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
            #Broadcast the row to Log Analytics
            print(f"CSV_EXPORT|{','.join(map(str, row))}")
            
            with open(self.filepath, mode='a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(row)


def disk_cleanup_daemon(store: IdempotencyStore):
    """Background thread that runs hourly to purge expired dbm keys."""
    while not shutdown_event.is_set():
        for _ in range(3600):
            if shutdown_event.is_set():
                return
            time.sleep(1)
        store.cleanup_expired()


class ServiceBusClientFactory:
    """
    Singleton factory to multiplex a single AMQP connection across multiple threads.
    Implements threading.Lock to satisfy Azure Python SDK concurrency rules.
    """
    _client = None
    _lock = threading.Lock() 

    @classmethod
    def get_client(cls, fully_qualified_namespace, credential):
        with cls._lock:
            if cls._client is None:
                # ARCHITECT PATCH: Fallback to Connection String if Entra ID is blocked
                conn_str = os.getenv("SERVICE_BUS_CONNECTION_STRING")
                if conn_str:
                    cls._client = ServiceBusClient.from_connection_string(conn_str, retry_total=3)
                else:
                    cls._client = ServiceBusClient(
                        fully_qualified_namespace, 
                        credential=credential,
                        retry_total=3
                    )
            return cls._client


def discover_target_queues(fqdn, credential):
    """
    Dual-Mode Discovery: Attempts to dynamically list queues via Administration Client.
    Gracefully degrades to the .env ASB_SOURCES array if RBAC permissions deny access.
    """
    enable_discovery = os.getenv("ENABLE_DYNAMIC_DISCOVERY", "False").lower() == "true"
    excluded_str = os.getenv("EXCLUDED_QUEUES", "")
    excluded_queues = [q.strip() for q in excluded_str.split(',')] if excluded_str else []
    parking_lot = os.getenv("PARKING_LOT_QUEUE_NAME", "parking-lot-queue")
    
    if enable_discovery:
        logger.info("Attempting dynamic queue discovery via ServiceBusAdministrationClient...")
        try:
            admin_client = ServiceBusAdministrationClient(fqdn, credential=credential)
            discovered_queues = []
            for q_properties in admin_client.list_queues():
                q_name = q_properties.name
                if q_name not in excluded_queues and q_name != parking_lot:
                    discovered_queues.append(q_name)
            logger.info(f"Dynamically discovered {len(discovered_queues)} eligible target queues.")
            return discovered_queues
        except HttpResponseError as e:
            if e.status_code == 403:
                logger.warning("RBAC 403 Forbidden: Agent identity lacks Data Owner permissions for dynamic discovery.")
                logger.warning("Gracefully degrading to static ASB_SOURCES array.")
            else:
                logger.error(f"Failed to execute dynamic discovery: {str(e)}")
    
    try:
        config_path = os.getenv("ASB_SOURCES_FILE", "data/asb_sources.json")
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                sources = json.load(f)
            return [source["name"] for source in sources if source.get("type") == "queue"]
        else:
            logger.error(f"Configuration file {config_path} not found.")
            return []
    except Exception as e:
        logger.error(f"CRITICAL: Failed to load ASB sources from file: {e}")
        return []


def drain_queue_dlq(queue_name, fqdn, credential, idempotency_store, classification_cache, ai_engine, db_client):
    """
    Worker function to drain available DLQ messages for a single queue.
    Exits once the DLQ is empty to free the thread for the next queue in the round-robin pool.
    """
    if shutdown_event.is_set():
        return
        
    prefetch = int(os.getenv("PREFETCH_COUNT", 20))
    max_count = int(os.getenv("ASB_MAX_MESSAGE_COUNT", 10))
    max_wait = int(os.getenv("ASB_MAX_WAIT_TIME", 5))
    parking_lot_name = os.getenv("PARKING_LOT_QUEUE_NAME", "parking-lot-queue")
    
    try:
        # ARCHITECT FIX: Moved client factory inside the try block to ensure thread resilience
        sb_client = ServiceBusClientFactory.get_client(fqdn, credential)
        
        dlq_receiver = sb_client.get_queue_receiver(
            queue_name=queue_name, 
            sub_queue=ServiceBusSubQueue.DEAD_LETTER,
            receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
            prefetch_count=prefetch
        )
        main_sender = sb_client.get_queue_sender(queue_name=queue_name)
        parking_lot_sender = sb_client.get_queue_sender(queue_name=parking_lot_name)
        
        classifier = AutonomousDLQClassifier(
            idempotency_cache=idempotency_store,
            classification_cache=classification_cache,
            ai_client=ai_engine,
            database_client=db_client,
            parking_lot_sender=parking_lot_sender,
            main_queue_sender=main_sender,
            dlq_receiver=dlq_receiver,
            source_queue_name=queue_name
        )
        
        with dlq_receiver, main_sender, parking_lot_sender:
            while not shutdown_event.is_set():
                messages = dlq_receiver.receive_messages(
                    max_message_count=max_count, 
                    max_wait_time=max_wait
                )
                
                if not messages:
                    break
                    
                logger.info(f"Found {len(messages)} anomalies in {queue_name}. Processing...")
                classifier.process_batch(messages)
                
    except Exception as e:
        logger.error(f"Critical error draining DLQ for {queue_name}: {str(e)}", exc_info=True)


def main():
    load_dotenv()
    fqdn = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    if not fqdn:
        logger.error("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE is missing.")
        return

    logger.info("Booting Autonomous DLQ Agent Orchestrator...")
    credential = DefaultAzureCredential()

    idempotency_store = IdempotencyStore()
    classification_cache = ClassificationCache()
    ai_engine = AIEngineFactory.get_engine()
    db_client = DemoTerminalDatabase()

    cleanup_thread = threading.Thread(target=disk_cleanup_daemon, args=(idempotency_store,), daemon=True)
    cleanup_thread.start()

    target_queues = discover_target_queues(fqdn, credential)
    if not target_queues:
        logger.error("No valid target queues discovered or configured. Shutting down.")
        return

    max_workers = int(os.getenv("MAX_CONCURRENT_QUEUES", 5))
    cycle_sleep = int(os.getenv("AGENT_CYCLE_SLEEP_SECONDS", 60))
    logger.info(f"Starting Round-Robin Poller for {len(target_queues)} queues across {max_workers} threads.")

    # CRITICAL PATCH: ThreadPoolExecutor elevated OUTSIDE the endless loop to prevent thread churn.
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Endless Orchestration Loop
        while not shutdown_event.is_set():
            futures = {
                executor.submit(
                    drain_queue_dlq, 
                    q_name, fqdn, credential, idempotency_store, classification_cache, ai_engine, db_client
                ): q_name for q_name in target_queues
            }
            
            for future in as_completed(futures):
                q_name = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.error(f"Thread processing {q_name} generated an exception: {exc}")
                    
            # Sleep before starting the next complete cycle to prevent AMQP link churn
            if not shutdown_event.is_set():
                logger.info(f"Cycle complete. Agent sleeping for {cycle_sleep} seconds.")
                time.sleep(cycle_sleep)

    logger.info("Agent shutdown complete. Closing shared AMQP connection pool...")
    
    client = ServiceBusClientFactory._client
    if client:
        try:
            client.close()
        except Exception as e:
            logger.error(f"Error closing ServiceBusClient: {e}")

if __name__ == "__main__":
    main()