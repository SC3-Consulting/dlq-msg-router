"""
This module implements the main entry point for the Autonomous DLQ Agent Orchestrator.
- It initialises the runtime environment, including logging, health monitoring, and observability metrics.
"""

import csv
import json
import logging
import os
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Union

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode, ServiceBusSubQueue
from azure.servicebus.management import ServiceBusAdministrationClient
from dotenv import load_dotenv

from src.ai_client import AIEngineFactory
from src.autonomous_dlq_classifier import AutonomousDLQClassifier
from src.resilience import CircuitBreaker, CircuitBreakerOpenError, backoff_sleep
from src.state_managers import ClassificationCache, IdempotencyStore

# Configure enterprise logging standard
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s",
)
logger = logging.getLogger("AgentOrchestrator")
# Azure SDK can emit very high-volume connection state logs at INFO; keep these at WARNING.
logging.getLogger("azure").setLevel(logging.WARNING)

# Global event to signal all threads to shut down gracefully
shutdown_event = threading.Event()


def _sanitise_metric_label(value: str) -> str:
    """Sanitises a string to be used as a Prometheus metric label by converting to lowercase, replacing non-alphanumeric characters with underscores, and stripping leading/trailing underscores.
    Args:
        value (str): The string to sanitise.
    Returns:
        str: The sanitised string.
    """
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value)).strip("_")


@dataclass
class RuntimeHealthState:
    """Tracks the runtime health and readiness of the agent, including uptime, readiness, shutdown requests, and last error encountered.
    Attributes:
        started_at (float): Timestamp when the agent started.
        ready_event (threading.Event): Event indicating if the agent is ready to process messages.
        shutdown_requested (threading.Event): Event indicating if a shutdown has been requested.
        last_error (Optional[str]): The last error message encountered, if any.
        lock (threading.Lock): Lock to ensure thread-safe access to the health state.
    """

    started_at: float = field(default_factory=time.time)
    ready_event: threading.Event = field(default_factory=threading.Event)
    shutdown_requested: threading.Event = field(default_factory=threading.Event)
    last_error: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def mark_ready(self):
        """Marks the agent as ready to process messages by setting the ready_event."""
        self.ready_event.set()

    def mark_shutdown(self, reason: Optional[str] = None):
        """Marks the agent as shutting down and optionally records a reason.
        Args:
            reason (Optional[str]): The reason for the shutdown, if any.
        """
        with self.lock:
            self.shutdown_requested.set()
            if reason:
                self.last_error = reason

    def mark_error(self, error: Union[Exception, str]):
        """Records an error encountered by the agent.
        Args:
            error (Union[Exception, str]): The error to record.
        """
        with self.lock:
            self.last_error = str(error)

    def reset(self):
        """Resets the runtime health state to its initial values."""
        with self.lock:
            self.started_at = time.time()
            self.ready_event.clear()
            self.shutdown_requested.clear()
            self.last_error = None
        shutdown_event.clear()

    def snapshot(self):
        """Returns a snapshot of the current runtime health state."""
        with self.lock:
            return {
                "ready": self.ready_event.is_set()
                and not self.shutdown_requested.is_set(),
                "shutdown_requested": self.shutdown_requested.is_set(),
                "uptime_seconds": round(time.time() - self.started_at, 3),
                "last_error": self.last_error,
            }


runtime_health = RuntimeHealthState()


@dataclass
class ObservabilityCollector:
    """Collects and aggregates observability metrics for the agent, including counters for processed messages, failures, and queue-specific metrics.
    Attributes:
        lock (threading.Lock): Lock to ensure thread-safe access to the metrics.
        counters (dict): Dictionary to store general metrics counters.
        queue_counters (dict): Dictionary to store queue-specific metrics counters.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    counters: dict = field(default_factory=dict)
    queue_counters: dict = field(default_factory=dict)

    def reset(self):
        """Resets the observability counters to their initial values."""
        with self.lock:
            self.counters = {}
            self.queue_counters = {}

    def increment(self, metric_name: str, amount: int = 1):
        """Increments a general metric counter by a specified amount.
        Args:
            metric_name (str): The name of the metric to increment.
            amount (int): The amount to increment the metric by.
        """
        with self.lock:
            self.counters[metric_name] = self.counters.get(metric_name, 0) + amount

    def increment_queue(self, queue_name: str, amount: int = 1):
        """Increments a queue-specific metric counter by a specified amount.
        Args:
            queue_name (str): The name of the queue metric to increment.
            amount (int): The amount to increment the metric by.
        """
        with self.lock:
            self.queue_counters[queue_name] = (
                self.queue_counters.get(queue_name, 0) + amount
            )

    def record_contract(self, contract: dict):
        """Records metrics based on the provided contract dictionary, incrementing relevant counters for processed messages, queue names, statuses, and suggested actions.
        Args:
            contract (dict): The contract dictionary containing metrics information.
        """
        self.increment("messages_processed_total")
        queue_name = contract.get("source_queue")
        if queue_name:
            self.increment_queue(queue_name)

        status = contract.get("status")
        if status:
            self.increment(f"status_{_sanitise_metric_label(status)}_total")

        suggested_action = contract.get("suggested_action")
        if suggested_action:
            action_key = _sanitise_metric_label(suggested_action)
            self.increment(f"action_{action_key}_total")
            if suggested_action in {"retry", "fix_and_retry"}:
                self.increment("retries_total")
            if suggested_action == "escalate":
                self.increment("escalations_total")

        if status == "Auto_Classified_From_Cache":
            self.increment("cache_hits_total")

        if status and status.startswith("AI_"):
            self.increment("ai_calls_total")

        if status in {
            "Quarantined",
            "AI_Suggested_Rule_Pending_Approval",
            "AI_Low_Confidence_Manual_Review",
        }:
            self.increment("escalations_total")

    def record_failure(self, category: str):
        """Records a failure metric for a specific category.
        Args:
            category (str): The category of the failure.
        """
        self.increment("failures_total")
        self.increment(f"failure_{_sanitise_metric_label(category)}_total")

    def snapshot(self):
        """Returns a snapshot of the current observability metrics."""
        with self.lock:
            return {
                "counters": dict(self.counters),
                "queues": dict(self.queue_counters),
            }


observability = ObservabilityCollector()


class HealthProbeHandler(BaseHTTPRequestHandler):
    """HTTP handler for health and metrics endpoints, responding to /health, /ready, and /metrics requests with appropriate JSON payloads and status codes.
    Attributes:
        path (str): The requested path.
    Methods:
        do_GET(self): Handles GET requests for /health, /ready, and /metrics endpoints.
    """

    def do_GET(self):
        """Handles GET requests for health and metrics endpoints."""
        if self.path not in {"/health", "/ready", "/metrics"}:
            self.send_error(404)
            return

        if self.path == "/metrics":
            body = json.dumps(observability.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        snapshot = runtime_health.snapshot()
        if self.path == "/ready" and not snapshot["ready"]:
            status_code = 503
        else:
            status_code = 200

        body = json.dumps({"path": self.path, **snapshot}).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Overrides the default logging to use the standard logger instead of printing to stderr."""
        return


def start_health_server(host: str, port: int):
    """Starts an HTTP server in a separate thread to serve health and metrics endpoints.
    Args:
        host (str): The host address to bind the server to.
        port (int): The port number to bind the server to.
    Returns:
        tuple: A tuple containing the HTTPServer instance and the thread running the server.
    """
    server = HTTPServer((host, port), HealthProbeHandler)
    thread = threading.Thread(
        target=server.serve_forever, name="health-probe", daemon=True
    )
    thread.start()
    return server, thread


def stop_health_server(server: Optional[HTTPServer]):
    """Stops the HTTP health server if it is running.
    Args:
        server (Optional[HTTPServer]): The HTTPServer instance to stop.
    """
    if server:
        server.shutdown()
        server.server_close()


def signal_handler(signum, frame):
    """Handles termination signals (SIGINT, SIGTERM) to initiate a graceful shutdown of the agent.
    Args:
        signum (int): The signal number.
        frame (frame): The current stack frame.
    """
    logger.warning(
        "\n[SIGINT] Termination signal detected. Initiating graceful shutdown..."
    )
    logger.warning(
        "Please wait for threads to finish their current ASB batch to prevent orphaned locks."
    )
    runtime_health.mark_shutdown("signal_received")
    shutdown_event.set()


signal.signal(signal.SIGINT, signal_handler)


class DemoTerminalDatabase:
    """Mock database to print terminal output AND generate a CSV dashboard for MVP.
    Attributes:
        filepath (str): The path to the CSV file for storing telemetry data.
        headers (list): The list of column headers for the CSV file.
        lock (threading.Lock): A lock to ensure thread-safe access to the CSV file.

    Methods:
        log_telemetry(self, contract): Logs telemetry data to the terminal and CSV file.
    """

    def __init__(self, filepath=None):
        """Initialises the DemoTerminalDatabase with a specified CSV file path or defaults to the TELEMETRY_CSV_PATH environment variable.
        Args:
            filepath (str, optional): The path to the CSV file for storing telemetry data. Defaults to None.
        """
        self.filepath = filepath or os.getenv(
            "TELEMETRY_CSV_PATH", "reports/telemetry_dashboard.csv"
        )
        self.headers = [
            "timestamp",
            "source_queue",
            "client_id",
            "message_type",
            "classification",
            "pattern",
            "status",
            "occurrence_count",
            "suggested_action",
            "confidence_score",
        ]
        self.lock = threading.Lock()

        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        if not os.path.exists(self.filepath):
            with open(self.filepath, mode="w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.headers)

    def log_telemetry(self, contract):
        """Logs telemetry data to the terminal and CSV file.
        Args:
            contract (dict): The contract dictionary containing telemetry data.
        """
        with self.lock:
            print(f"\n[{contract['status'].upper()}] - {contract['classification']}")
            print(f" -> Pattern: {contract['pattern']}")
            if "suggested_action" in contract:
                print(f" -> Action:  {contract['suggested_action']}")
            print("-" * 50)

            observability.record_contract(contract)
            logger.info(f"JSON_EXPORT|{json.dumps(contract, sort_keys=True)}")

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
                contract.get("confidence_score", "N/A"),
            ]
            # Broadcast the row to Log Analytics
            print(f"CSV_EXPORT|{','.join(map(str, row))}")

            with open(self.filepath, mode="a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(row)


def disk_cleanup_daemon(store: IdempotencyStore):
    """Background thread that runs hourly to purge expired dbm keys.
    Args:
        store (IdempotencyStore): The idempotency store instance to clean up.
    """
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
    Attributes:
        _clients (Dict[str, ServiceBusClient]): A dictionary to store ServiceBusClient instances keyed by namespace.
        _client (Optional[ServiceBusClient]): The default ServiceBusClient instance.
        _lock (threading.Lock): A lock to ensure thread-safe access to the client instances.
    Methods:
        get_client(cls, fully_qualified_namespace, credential): Returns a ServiceBusClient instance for the specified namespace, creating one if it doesn't exist.
    """

    _clients: Dict[str, ServiceBusClient] = {}
    _client = None
    _lock = threading.Lock()

    @classmethod
    def get_client(cls, fully_qualified_namespace, credential):
        key = fully_qualified_namespace or "__conn_str__"
        with cls._lock:
            if key not in cls._clients:
                # Fallback to connection string only when it matches the requested namespace
                # (or when namespace is omitted), so multi-namespace mode can use Entra ID cleanly.
                conn_str = os.getenv("SERVICE_BUS_CONNECTION_STRING")
                conn_str_namespace = (
                    _extract_namespace_from_connection_string(conn_str)
                    if conn_str
                    else None
                )
                if conn_str and (
                    not fully_qualified_namespace
                    or conn_str_namespace == fully_qualified_namespace
                ):
                    cls._clients[key] = ServiceBusClient.from_connection_string(
                        conn_str, retry_total=3
                    )
                else:
                    cls._clients[key] = ServiceBusClient(
                        fully_qualified_namespace, credential=credential, retry_total=3
                    )
                if cls._client is None:
                    cls._client = cls._clients[key]
            return cls._clients[key]


def _extract_namespace_from_connection_string(conn_str: str) -> Optional[str]:
    """Extracts the fully qualified namespace from a Service Bus connection string.
    Args:
        conn_str (str): The Service Bus connection string.
    Returns:
        Optional[str]: The fully qualified namespace if found, otherwise None.
    """
    if not conn_str:
        return None

    match = re.search(r"Endpoint\s*=\s*sb://([^/;]+)", conn_str, re.IGNORECASE)
    if not match:
        return None

    return match.group(1).strip()


def _resolve_namespace_targets(
    conn_str: Optional[str], fqdn: Optional[str], fqdn_list_raw: Optional[str]
) -> List[str]:
    """
    Resolves target namespaces with precedence:
    1) SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES (comma-separated)
    2) SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE
    3) namespace derived from SERVICE_BUS_CONNECTION_STRING
    Returns a list of unique, non-empty namespaces.

    Args:
        conn_str (Optional[str]): The Service Bus connection string.
        fqdn (Optional[str]): The fully qualified domain name of the Service Bus namespace.
        fqdn_list_raw (Optional[str]): A raw comma-separated string of fully qualified domain names.
    Returns:
        List[str]: A list of unique, non-empty fully qualified namespaces.
    """
    raw = (fqdn_list_raw or "").strip()
    if raw:
        # Preserve order while deduplicating.
        seen = set()
        namespaces = []
        for item in [n.strip() for n in raw.split(",") if n.strip()]:
            if item not in seen:
                seen.add(item)
                namespaces.append(item)
        return namespaces

    resolved_fqdn = fqdn
    if not resolved_fqdn and conn_str:
        resolved_fqdn = _extract_namespace_from_connection_string(conn_str)

    return [resolved_fqdn] if resolved_fqdn else []


def discover_target_queues(fqdn, credential):
    """
    Dual-Mode Discovery: Attempts to dynamically list queues via Administration Client.
    Gracefully degrades to the .env ASB_SOURCES array if RBAC permissions deny access.

    Args:
        fqdn (str): The fully qualified domain name of the Service Bus namespace.
        credential: The Azure credential to use for authentication.
    Returns:
        List[str]: A list of discovered queue names.
    """

    def _load_static_sources():
        """Loads queue names from the ASB_SOURCES environment variable or a JSON file.
        Returns:
            List[str]: A list of queue names.
        """
        sources_json = os.getenv("ASB_SOURCES")
        if sources_json:
            try:
                sources = json.loads(sources_json)
                return [
                    source["name"]
                    for source in sources
                    if source.get("type") == "queue"
                ]
            except Exception as e:
                logger.error(
                    f"CRITICAL: Failed to parse ASB_SOURCES from environment: {e}"
                )
                return []

        config_path = os.getenv("ASB_SOURCES_FILE", "data/asb_sources.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    sources = json.load(f)
                return [
                    source["name"]
                    for source in sources
                    if source.get("type") == "queue"
                ]
            except Exception as e:
                logger.error(
                    f"CRITICAL: Failed to load ASB sources from file {config_path}: {e}"
                )
                return []

        logger.error(f"Configuration file {config_path} not found.")
        return []

    enable_discovery = os.getenv("ENABLE_DYNAMIC_DISCOVERY", "False").lower() == "true"
    excluded_str = os.getenv("EXCLUDED_QUEUES", "")
    excluded_queues = (
        [q.strip() for q in excluded_str.split(",")] if excluded_str else []
    )
    parking_lot = os.getenv("PARKING_LOT_QUEUE_NAME", "parking-lot-queue")

    if enable_discovery:
        logger.info(
            "Attempting dynamic queue discovery via ServiceBusAdministrationClient..."
        )
        try:
            conn_str = os.getenv("SERVICE_BUS_CONNECTION_STRING")
            if conn_str:
                admin_client = ServiceBusAdministrationClient.from_connection_string(
                    conn_str
                )
            else:
                admin_client = ServiceBusAdministrationClient(
                    fqdn, credential=credential
                )

            discovered_queues = []
            for q_properties in admin_client.list_queues():
                q_name = q_properties.name
                if q_name not in excluded_queues and q_name != parking_lot:
                    discovered_queues.append(q_name)
            logger.info(
                f"Dynamically discovered {len(discovered_queues)} eligible target queues."
            )
            return discovered_queues
        except HttpResponseError as e:
            if e.status_code == 403:
                logger.warning(
                    "RBAC 403 Forbidden: Agent identity lacks Data Owner permissions for dynamic discovery."
                )
                logger.warning("Gracefully degrading to static ASB_SOURCES array.")
            else:
                logger.error(f"Failed to execute dynamic discovery: {str(e)}")

    return _load_static_sources()


def drain_queue_dlq(
    queue_name,
    fqdn,
    credential,
    idempotency_store,
    classification_cache,
    ai_engine,
    db_client,
):
    """
    Worker function to drain available DLQ messages for a single queue.
    Exits once the DLQ is empty to free the thread for the next queue in the round-robin pool.
    Uses a per-queue circuit breaker to stop hammering unavailable broker connections,
    and exponential backoff with jitter on transient failures.

    Args:
        queue_name (str): The name of the queue whose DLQ is to be drained.
        fqdn (str): The fully qualified domain name of the Service Bus namespace.
        credential: The Azure credential to use for authentication.
        idempotency_store (IdempotencyStore): The idempotency store instance for tracking processed messages.
        classification_cache (ClassificationCache): The classification cache instance for storing message classifications.
        ai_engine: The AI engine instance for classifying messages.
        db_client: The database client instance for logging telemetry data.
    Returns:
        None
    """
    if shutdown_event.is_set():
        return

    multi_namespace_mode = bool(
        os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES", "").strip()
    )
    circuit_name = (
        f"queue:{fqdn}:{queue_name}" if multi_namespace_mode else f"queue:{queue_name}"
    )
    circuit = CircuitBreaker(circuit_name)

    if not circuit.allow_request():
        logger.warning(
            f"[CircuitBreaker:{circuit_name}] OPEN — skipping drain cycle to protect broker."
        )
        observability.record_failure("circuit_open")
        return

    prefetch = int(os.getenv("PREFETCH_COUNT", 20))
    max_count = int(os.getenv("ASB_MAX_MESSAGE_COUNT", 10))
    max_wait = int(os.getenv("ASB_MAX_WAIT_TIME", 5))
    parking_lot_name = os.getenv("PARKING_LOT_QUEUE_NAME", "parking-lot-queue")
    drain_max_attempts = int(os.getenv("DRAIN_RETRY_MAX_ATTEMPTS", "3"))
    drain_backoff_base = float(os.getenv("DRAIN_BACKOFF_BASE_SECONDS", "1.0"))
    drain_backoff_max = float(os.getenv("DRAIN_BACKOFF_MAX_SECONDS", "30.0"))

    last_exc = None
    for attempt in range(drain_max_attempts):
        if shutdown_event.is_set():
            return
        try:
            sb_client = ServiceBusClientFactory.get_client(fqdn, credential)

            dlq_receiver = sb_client.get_queue_receiver(
                queue_name=queue_name,
                sub_queue=ServiceBusSubQueue.DEAD_LETTER,
                receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
                prefetch_count=prefetch,
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
                source_queue_name=queue_name,
            )

            with dlq_receiver, main_sender, parking_lot_sender:
                while not shutdown_event.is_set():
                    messages = dlq_receiver.receive_messages(
                        max_message_count=max_count, max_wait_time=max_wait
                    )

                    if not messages:
                        break

                    logger.info(
                        f"Found {len(messages)} anomalies in {queue_name}. Processing..."
                    )
                    classifier.process_batch(messages)

            # Successful drain — record success and exit retry loop
            circuit.record_success()
            return

        except ClientAuthenticationError as e:
            circuit.record_failure()
            _handle_auth_failure(queue_name, e)
            return

        except Exception as e:
            last_exc = e
            circuit.record_failure()
            observability.record_failure("queue_drain")
            if attempt < drain_max_attempts - 1 and not shutdown_event.is_set():
                sleep_dur = backoff_sleep(
                    attempt,
                    base_seconds=drain_backoff_base,
                    max_seconds=drain_backoff_max,
                )
                logger.warning(
                    f"Transient error draining {queue_name} (attempt {attempt + 1}/{drain_max_attempts}, "
                    f"backoff {sleep_dur:.1f}s): {e}"
                )
            else:
                logger.error(
                    f"Critical error draining DLQ for {queue_name} after {drain_max_attempts} attempts: {str(e)}",
                    exc_info=True,
                )


def _handle_auth_failure(queue_name: str, error: Exception):
    """
    Handles authentication failures by recording the failure, marking the runtime health state, and initiating a shutdown.
    Args:
        queue_name (str): The name of the queue where the authentication failure occurred.
        error (Exception): The exception that caused the authentication failure.
    """
    observability.record_failure("auth")
    runtime_health.mark_error(error)
    runtime_health.mark_shutdown("authentication_failure")
    shutdown_event.set()
    logger.error(
        (
            f"Authentication failed while draining {queue_name}. "
            "Configure SERVICE_BUS_CONNECTION_STRING for local testing, or provide a "
            "working Azure identity (managed identity, az login, or environment credentials)."
        ),
        exc_info=True,
    )


def _validate_startup_configuration(
    conn_str: Optional[str], fqdn: Optional[str], fqdn_list_raw: Optional[str] = None
):
    """Validates the startup configuration for the Autonomous DLQ Agent Orchestrator.
    Args:
        conn_str (Optional[str]): The Service Bus connection string.
        fqdn (Optional[str]): The fully qualified domain name of the Service Bus namespace.
        fqdn_list_raw (Optional[str]): A raw comma-separated string of fully qualified domain names.
    Returns:
        List[str]: A list of error messages if any configuration issues are found.
    """
    errors = []

    if not fqdn and not conn_str and not (fqdn_list_raw or "").strip():
        errors.append(
            "Either SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE or SERVICE_BUS_CONNECTION_STRING must be configured. "
            "Optionally SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES can be used for multi-namespace mode."
        )

    if conn_str and (fqdn_list_raw or "").strip():
        errors.append(
            "SERVICE_BUS_CONNECTION_STRING cannot be combined with SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES. "
            "Use managed identity with SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES for multi-namespace mode."
        )

    ai_provider = os.getenv("AI_PROVIDER", "OLLAMA").upper()
    if ai_provider == "AZURE_FOUNDRY":
        if not os.getenv("AZURE_FOUNDRY_ENDPOINT"):
            errors.append(
                "AZURE_FOUNDRY_ENDPOINT is required when AI_PROVIDER=AZURE_FOUNDRY."
            )
        if not os.getenv("AZURE_FOUNDRY_DEPLOYMENT_NAME"):
            errors.append(
                "AZURE_FOUNDRY_DEPLOYMENT_NAME is required when AI_PROVIDER=AZURE_FOUNDRY."
            )
    elif ai_provider == "OLLAMA":
        if not os.getenv("OLLAMA_MODEL"):
            errors.append("OLLAMA_MODEL is required when AI_PROVIDER=OLLAMA.")
        if not os.getenv("OLLAMA_ENDPOINT"):
            errors.append("OLLAMA_ENDPOINT is required when AI_PROVIDER=OLLAMA.")
    else:
        errors.append(
            f"AI_PROVIDER '{ai_provider}' is not supported. Use OLLAMA or AZURE_FOUNDRY."
        )

    enable_discovery = os.getenv("ENABLE_DYNAMIC_DISCOVERY", "False").lower() == "true"
    if not enable_discovery:
        sources_json = os.getenv("ASB_SOURCES")
        sources_file = os.getenv("ASB_SOURCES_FILE", "data/asb_sources.json")
        if not sources_json and not os.path.exists(sources_file):
            errors.append(
                "ENABLE_DYNAMIC_DISCOVERY is False, but neither ASB_SOURCES nor a valid ASB_SOURCES_FILE is available."
            )

    return errors


def main():
    """Main entry point for the Autonomous DLQ Agent Orchestrator.
    Initialises the runtime environment, validates configuration, discovers target queues, and starts the round-robin polling loop to drain DLQs.
    """
    load_dotenv()
    runtime_health.reset()
    observability.reset()

    conn_str = os.getenv("SERVICE_BUS_CONNECTION_STRING")
    fqdn = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    fqdn_list_raw = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES", "")

    namespace_targets = _resolve_namespace_targets(conn_str, fqdn, fqdn_list_raw)

    startup_errors = _validate_startup_configuration(conn_str, fqdn, fqdn_list_raw)
    if startup_errors:
        for error in startup_errors:
            logger.error(f"Startup configuration error: {error}")
        observability.record_failure("startup_configuration")
        runtime_health.mark_error("; ".join(startup_errors))
        return

    if not namespace_targets:
        logger.error(
            "No Service Bus namespace targets configured. Provide SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE, "
            "SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES, or a derivable SERVICE_BUS_CONNECTION_STRING."
        )
        observability.record_failure("missing_namespace")
        runtime_health.mark_error("No Service Bus namespace targets configured")
        return

    logger.info("Booting Autonomous DLQ Agent Orchestrator...")
    credential = DefaultAzureCredential()

    idempotency_store = IdempotencyStore()
    classification_cache = ClassificationCache()
    ai_engine = AIEngineFactory.get_engine()
    db_client = DemoTerminalDatabase()

    cleanup_thread = threading.Thread(
        target=disk_cleanup_daemon, args=(idempotency_store,), daemon=True
    )
    cleanup_thread.start()

    health_server = None
    health_thread = None
    if os.getenv("ENABLE_HEALTH_ENDPOINTS", "false").lower() == "true":
        health_host = os.getenv("HEALTHCHECK_HOST", "0.0.0.0")
        health_port = int(os.getenv("HEALTHCHECK_PORT", 8080))
        health_server, health_thread = start_health_server(health_host, health_port)
        logger.info(f"Health probes enabled on {health_host}:{health_port}.")

    target_queue_bindings = []
    for target_fqdn in namespace_targets:
        queues = discover_target_queues(target_fqdn, credential)
        if not queues:
            logger.warning(
                f"No eligible queues discovered for namespace {target_fqdn}."
            )
            continue
        for queue_name in queues:
            target_queue_bindings.append((target_fqdn, queue_name))

    if not target_queue_bindings:
        logger.error("No valid target queues discovered or configured. Shutting down.")
        observability.record_failure("no_target_queues")
        runtime_health.mark_error("No valid target queues discovered or configured")
        runtime_health.mark_shutdown("no_target_queues")
        stop_health_server(health_server)
        return

    runtime_health.mark_ready()

    max_workers = int(os.getenv("MAX_CONCURRENT_QUEUES", 5))
    cycle_sleep = int(os.getenv("AGENT_CYCLE_SLEEP_SECONDS", 60))
    shutdown_timeout = int(os.getenv("SHUTDOWN_TIMEOUT_SECONDS", 30))
    logger.info(
        f"Starting Round-Robin Poller for {len(target_queue_bindings)} queue bindings across "
        f"{len(namespace_targets)} namespaces and {max_workers} threads."
    )

    # CRITICAL PATCH: ThreadPoolExecutor elevated OUTSIDE the endless loop to prevent thread churn.
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Endless Orchestration Loop
        while not shutdown_event.is_set():
            futures = {
                executor.submit(
                    drain_queue_dlq,
                    q_name,
                    target_fqdn,
                    credential,
                    idempotency_store,
                    classification_cache,
                    ai_engine,
                    db_client,
                ): (target_fqdn, q_name)
                for target_fqdn, q_name in target_queue_bindings
            }

            for future in as_completed(futures):
                target_fqdn, q_name = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    observability.record_failure("worker_future")
                    logger.error(
                        f"Thread processing {target_fqdn}/{q_name} generated an exception: {exc}"
                    )

            # Sleep before starting the next complete cycle to prevent AMQP link churn
            if not shutdown_event.is_set():
                logger.info(
                    f"Cycle complete. Agent sleeping for {cycle_sleep} seconds."
                )
                time.sleep(cycle_sleep)

    logger.info("Agent shutdown complete. Closing shared AMQP connection pool...")

    runtime_health.mark_shutdown("shutdown_complete")
    for key, client in list(ServiceBusClientFactory._clients.items()):
        try:
            client.close()
        except Exception as e:
            logger.error(f"Error closing ServiceBusClient for key '{key}': {e}")
    ServiceBusClientFactory._clients.clear()
    ServiceBusClientFactory._client = None

    cleanup_thread.join(timeout=shutdown_timeout)
    if cleanup_thread.is_alive():
        logger.warning(
            "Cleanup thread did not stop within the configured shutdown timeout."
        )

    stop_health_server(health_server)


if __name__ == "__main__":
    main()
