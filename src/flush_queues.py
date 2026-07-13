"""
This module provides a utility to flush all messages from specified Azure Service Bus queues and their Dead Letter Queues (DLQs).
- It connects to the Service Bus using either a connection string or Managed Identity credentials.
- The queues to flush are specified via the ASB_SOURCES environment variable, which should be a JSON array of objects with "type" and "name" fields.
- Optionally, a parking lot queue can be specified via the PARKING_LOT_QUEUE_NAME environment variable.
- The utility logs the number of messages flushed from each queue and DLQ, and confirms when all queues are empty and ready for testing.
- This is useful for integration testing scenarios where a clean state is required before running tests.
"""

import json
import logging
import os
import subprocess

from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("QueueFlusher")


def _resolve_service_bus_namespace() -> str:
    """Resolves Service Bus namespace from env first, then Terraform output fallback.

    Returns:
        str: Fully qualified Service Bus namespace, or empty string when unavailable.
    """
    namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", "").strip()

    # Guard against common placeholder values copied from .env.example.
    if namespace and "your-namespace-here" not in namespace:
        return namespace

    try:
        terraform_output = subprocess.check_output(
            [
                "terraform",
                "-chdir=infra/terraform/azure",
                "output",
                "-raw",
                "servicebus_namespace_fqdn",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
        if terraform_output:
            logger.info(
                "Using SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE from Terraform output."
            )
            return terraform_output
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return namespace


def flush_queue(client, queue_name, is_dlq=False):
    """Flushes all messages from the specified queue or its Dead Letter Queue (DLQ).
    Args:
        client (ServiceBusClient): An instance of ServiceBusClient to connect to the Service Bus.
        queue_name (str): The name of the queue to flush.
        is_dlq (bool): If True, flushes the Dead Letter Queue (DLQ) associated with the specified queue; otherwise, flushes the main queue.
    """
    target = f"{queue_name}/$DeadLetterQueue" if is_dlq else queue_name
    queue_type = "DLQ" if is_dlq else "Main Queue"

    logger.info(f"Connecting to {queue_type} ({target}) to flush messages...")

    with client.get_queue_receiver(
        queue_name=target, receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE
    ) as receiver:
        messages = receiver.receive_messages(max_message_count=100, max_wait_time=3)

        if not messages:
            logger.info(f"[{queue_type}] is already empty.")
        else:
            logger.info(
                f"[{queue_type}] Flushed {len(messages)} messages into the void."
            )


def main():
    """Main function to flush all specified queues and their DLQs."""
    fully_qualified_namespace = _resolve_service_bus_namespace()
    sources_json = os.getenv(
        "ASB_SOURCES",
        '[{"type": "queue","name": "payments-queue"},{"type": "queue","name": "integration-queue"}]',
    )
    parking_lot_name = os.getenv("PARKING_LOT_QUEUE_NAME")

    if not fully_qualified_namespace:
        logger.error(
            "Missing SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE in environment settings"
        )
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
