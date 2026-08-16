"""Entry point for the isolated notification/webhook worker."""

import logging
import os
import time

from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusReceiveMode, ServiceBusSubQueue
from dotenv import load_dotenv

from src.notification import NotificationWorker, WebhookRegistry
from src.run_agent import ServiceBusClientFactory, _resolve_namespace_targets

logger = logging.getLogger("NotificationWorker")


def main():
    """Run the notification worker, processing messages from the Service Bus queue."""
    load_dotenv()
    namespace = os.getenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE")
    connection_string = os.getenv("SERVICE_BUS_CONNECTION_STRING")
    namespaces = _resolve_namespace_targets(connection_string, namespace, "")
    if not namespaces:
        raise RuntimeError("A Service Bus namespace or connection string is required")

    notification_queue = os.getenv("NOTIFICATION_QUEUE_NAME", "notification-queue")
    manual_queue = os.getenv(
        "NOTIFICATION_MANUAL_QUEUE_NAME", "notification-manual-queue"
    )
    client = ServiceBusClientFactory.get_client(namespaces[0], DefaultAzureCredential())
    receiver = client.get_queue_receiver(
        queue_name=notification_queue,
        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
        prefetch_count=int(os.getenv("NOTIFICATION_PREFETCH_COUNT", "10")),
    )
    manual_sender = client.get_queue_sender(queue_name=manual_queue)
    worker = NotificationWorker(receiver, manual_sender, WebhookRegistry())
    max_wait = int(os.getenv("NOTIFICATION_MAX_WAIT_TIME", "5"))

    with receiver, manual_sender:
        while True:
            messages = receiver.receive_messages(
                max_message_count=int(
                    os.getenv("NOTIFICATION_MAX_MESSAGE_COUNT", "10")
                ),
                max_wait_time=max_wait,
            )
            if not messages:
                if os.getenv("NOTIFICATION_RUN_ONCE", "false").lower() == "true":
                    return
                time.sleep(float(os.getenv("NOTIFICATION_IDLE_SLEEP_SECONDS", "2")))
                continue
            for message in messages:
                try:
                    worker.process(message)
                except Exception:
                    logger.exception(
                        "Notification processing failed; leaving event for broker redelivery"
                    )


if __name__ == "__main__":
    main()
