"""Durable notification events and webhook delivery helpers."""

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
from azure.servicebus import ServiceBusMessage

NOTIFICATION_SCHEMA_VERSION = "1.0"


class PermanentWebhookError(Exception):
    """Raised when a webhook returns a permanent HTTP 4xx response."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Webhook returned permanent HTTP {status_code}")


class TransientWebhookError(Exception):
    """Raised when webhook delivery may succeed on a later attempt."""


@dataclass(frozen=True)
class WebhookConfig:
    """Represents the configuration for a webhook endpoint."""

    client_id: str
    endpoint: str
    secret: str
    enabled: bool = True
    registry_version: str = "unknown"


class WebhookRegistry:
    """Loads non-secret endpoint metadata and secrets from environment configuration.

    Production deployments should replace this adapter with a Key Vault-backed provider.
    The secret is deliberately never serialised into a notification event.
    """

    def __init__(self, raw_config: Optional[str] = None, provider=None):
        self._raw_config = raw_config or os.getenv("WEBHOOK_REGISTRY_JSON", "{}")
        self._provider = provider or self._build_app_configuration_provider()

    @staticmethod
    def _build_app_configuration_provider():
        """Build an App Configuration provider if the required environment variables are set."""
        endpoint = os.getenv("APP_CONFIGURATION_ENDPOINT", "").strip()
        vault_url = os.getenv("WEBHOOK_SECRETS_VAULT_URL", "").strip()
        if not endpoint or not vault_url:
            return None
        try:
            from azure.appconfiguration import AzureAppConfigurationClient
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            credential = DefaultAzureCredential()
            return AppConfigurationRegistryProvider(
                AzureAppConfigurationClient(endpoint, credential),
                SecretClient(vault_url=vault_url, credential=credential),
                label=os.getenv("APP_CONFIGURATION_LABEL", ""),
            )
        except ImportError:
            logging.getLogger(__name__).exception(
                "Azure registry dependencies are unavailable"
            )
            return None

    def get(self, client_id: str) -> Optional[WebhookConfig]:
        """Return the webhook configuration for the given client_id, or None if not found.

        Args:
            client_id: The identifier of the client for which to retrieve the webhook configuration.

        Returns:
            Optional[WebhookConfig]: The webhook configuration for the given client_id, or None if not found.
        """
        if self._provider is not None:
            return self._provider.get(client_id)
        try:
            registry = json.loads(self._raw_config or "{}")
            item = registry.get(client_id)
            if not item or not item.get("endpoint") or not item.get("secret"):
                return None
            return WebhookConfig(
                client_id=client_id,
                endpoint=item["endpoint"],
                secret=item["secret"],
                enabled=item.get("enabled", True),
                registry_version=str(item.get("version", "unknown")),
            )
        except (TypeError, json.JSONDecodeError, AttributeError):
            return None


class AppConfigurationRegistryProvider:
    """App Configuration metadata provider with Key Vault secret resolution.

    The provider boundary allows replacing this implementation with blob, SQL, or
    another registry without changing NotificationWorker.
    """

    def __init__(
        self, configuration_client, secret_client, key="webhook-registry", label=""
    ):
        """Initialise the provider with the given clients and configuration key/label."""
        self.configuration_client = configuration_client
        self.secret_client = secret_client
        self.key = key
        self.label = label

    def get(self, client_id: str) -> Optional[WebhookConfig]:
        """Return the webhook configuration for the given client_id, or None if not found.

        Args:
            client_id: The identifier of the client for which to retrieve the webhook configuration.

        Returns:
            Optional[WebhookConfig]: The webhook configuration for the given client_id, or None if not found.
        """
        try:
            setting = self.configuration_client.get_configuration_setting(
                key=self.key, label=self.label or "\0"
            )
            registry = json.loads(setting.value or "{}")
            item = registry.get(client_id)
            secret_name = item.get("secret_name") if item else None
            if not item or not item.get("endpoint") or not secret_name:
                return None
            secret = self.secret_client.get_secret(secret_name)
            return WebhookConfig(
                client_id=client_id,
                endpoint=item["endpoint"],
                secret=secret.value,
                enabled=item.get("enabled", True),
                registry_version=str(item.get("version", setting.etag or "unknown")),
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Webhook registry lookup failed for client %s", client_id
            )
            return None


def _property(properties: Optional[Dict[Any, Any]], *names: str) -> Optional[str]:
    """Return the first non-empty property value for the given names.

    Args:
        properties: A dictionary of message properties.
        names: A list of property names to check.

    Returns: The first non-empty property value found, or None if none are found.
    """
    for name in names:
        for key in (name, name.encode("utf-8")):
            if properties and key in properties and properties[key] is not None:
                value = properties[key]
                return value.decode("utf-8") if isinstance(value, bytes) else str(value)
    return None


def build_notification_event(
    message: Any, pattern: str, source_queue: Optional[str] = None
) -> Dict[str, Any]:
    """Build a redacted, versioned event suitable for durable queue publication.

    Args:
        message: The original Service Bus message that triggered the event.
        pattern: A string indicating the reason for the event (e.g., "payload_exceeds_broker_limits").
        source_queue: Optional name of the source queue; if not provided, it will be extracted from the message properties.

    Returns: A dictionary representing the notification event.
    """
    properties = getattr(message, "application_properties", None) or {}
    message_id = str(getattr(message, "message_id", "unknown"))
    correlation_id = getattr(message, "correlation_id", None) or _property(
        properties, "correlation_id", "Correlation-Id"
    )
    client_id = _property(properties, "client_id", "Client-Id") or "unknown_client"
    event_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"dlq-notification:{message_id}:{pattern}")
    )
    return {
        "schema_version": NOTIFICATION_SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": "DeadLetterMessageDropped",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "client_id": client_id,
        "source_queue": source_queue
        or _property(properties, "source_queue")
        or "unknown_queue",
        "source_message_id": message_id,
        "correlation_id": str(correlation_id) if correlation_id else None,
        "dlq_reason": getattr(message, "dead_letter_reason", None) or "Unknown",
        "dlq_description": getattr(message, "dead_letter_error_description", None)
        or "Unknown",
        "pattern": pattern,
        "classification": (
            "Capacity_Limit_Exceeded"
            if pattern == "payload_exceeds_broker_limits"
            else None
        ),
        "delivery_mode": "pending",
        "webhook_configured": None,
        "webhook_attempted": False,
        "manual_reason": None,
        "attempt_count": 0,
        "replay_count": 0,
    }


def canonical_json(event: Dict[str, Any]) -> bytes:
    """Return a canonical JSON representation of the event for signing.

    Args:
        event: The event dictionary to serialise.

    Returns: A bytes object containing the canonical JSON representation of the event.
    """
    return json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_event(
    event: Dict[str, Any], secret: str, timestamp: Optional[str] = None
) -> str:
    """Generate a HMAC-SHA256 signature for the event using the provided secret and timestamp.

    Args:
        event: The event dictionary to sign.
        secret: The secret key used for signing.
        timestamp: Optional timestamp string; if not provided, the current time will be used.

    Returns: A string containing the signature in the format "t=<timestamp>,v1=<signature>".
    """
    timestamp = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + canonical_json(event),
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def deliver_webhook(
    event: Dict[str, Any], config: WebhookConfig, timeout_seconds: float = 10.0
) -> None:
    """Deliver an event, distinguishing permanent 4xx from transient failures.

    Args:
        event: The event dictionary to deliver.
        config: The WebhookConfig containing the endpoint and secret.
        timeout_seconds: The timeout for the HTTP request.
    Raises:
        PermanentWebhookError: If the webhook returns a 4xx response.
        TransientWebhookError: If the webhook request fails or returns a non-4xx response.
    """
    endpoint = config.endpoint
    if (
        not endpoint.lower().startswith("https://")
        and os.getenv("ALLOW_INSECURE_WEBHOOKS", "false").lower() != "true"
    ):
        raise PermanentWebhookError(400)

    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Notification-Event-Id": event["event_id"],
        "X-Notification-Timestamp": timestamp,
        "X-Notification-Signature": sign_event(event, config.secret, timestamp),
    }
    try:
        response = requests.post(
            endpoint,
            data=canonical_json(event),
            headers=headers,
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise TransientWebhookError(str(exc)) from exc

    if 200 <= response.status_code < 300:
        return
    if 400 <= response.status_code < 500:
        raise PermanentWebhookError(response.status_code)
    raise TransientWebhookError(f"Webhook returned HTTP {response.status_code}")


class NotificationWorker:
    """Processes notification events and sends permanent failures to manual handling.

    Attributes:
        receiver: The Service Bus receiver for incoming messages.
        manual_sender: The Service Bus sender for manual handling messages.
        registry: The WebhookRegistry for looking up webhook configurations.
    """

    def __init__(
        self, receiver, manual_sender, registry: Optional[WebhookRegistry] = None
    ):
        self.receiver = receiver
        self.manual_sender = manual_sender
        self.registry = registry or WebhookRegistry()
        self.logger = logging.getLogger(self.__class__.__name__)

    def _manual_event(
        self, event: Dict[str, Any], reason: str, status: Optional[int] = None
    ):
        """Prepare a manual event with the given reason and optional HTTP status code.

        Args:
            event: The original event dictionary.
            reason: The reason for manual handling (e.g., "no_webhook_mapping", "webhook_4xx_permanent_failure").
            status: The optional HTTP status code if applicable.
        Returns:
            A new event dictionary updated for manual handling.
        """
        updated = dict(event)
        updated.update(
            {
                "delivery_mode": "manual",
                "webhook_attempted": bool(event.get("webhook_configured")),
                "manual_reason": reason,
                "webhook_http_status": status,
                "manual_required_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return updated

    def process(self, message: Any) -> str:
        """Process a single notification message, delivering to webhook or manual queue as needed.

        Args:
            message: The Service Bus message to process.
        Returns:
            A string indicating the outcome: "delivered" for successful webhook delivery, or "manual" if sent to manual handling.
        Raises:
            TransientWebhookError: If webhook delivery fails transiently, leaving the message for redelivery.
        """
        event = json.loads(b"".join(message.body).decode("utf-8"))
        config = self.registry.get(event.get("client_id", ""))
        event["webhook_configured"] = bool(config and config.enabled)
        event["registry_version"] = config.registry_version if config else "not_found"
        event["attempt_count"] = int(event.get("attempt_count", 0)) + 1

        if not config or not config.enabled:
            manual = self._manual_event(event, "no_webhook_mapping")
            self.manual_sender.send_messages(
                ServiceBusMessage(
                    json.dumps(manual).encode("utf-8"),
                    content_type="application/json",
                    subject="ManualNotificationRequired",
                    message_id=manual["event_id"],
                )
            )
            self.receiver.complete_message(message)
            return "manual"

        event["webhook_attempted"] = True
        try:
            deliver_webhook(event, config)
        except PermanentWebhookError as exc:
            manual = self._manual_event(
                event, "webhook_4xx_permanent_failure", exc.status_code
            )
            self.manual_sender.send_messages(
                ServiceBusMessage(
                    json.dumps(manual).encode("utf-8"),
                    content_type="application/json",
                    subject="ManualNotificationRequired",
                    message_id=manual["event_id"],
                )
            )
            self.receiver.complete_message(message)
            return "manual"
        except TransientWebhookError:
            self.logger.warning(
                "Transient webhook failure for %s", event.get("event_id")
            )
            raise

        self.receiver.complete_message(message)
        return "delivered"


def replay_manual_event(
    event: Dict[str, Any], operator: str, reason: str
) -> Dict[str, Any]:
    """Prepare an authorised manual event for replay to the automated queue.

    Args:
        event: The original manual event dictionary.
        operator: The identifier of the operator requesting the replay.
        reason: The reason for the replay request.

    Returns: A new event dictionary updated for replay to the automated queue.
    """
    if not operator.strip():
        raise ValueError("operator is required")
    replay = dict(event)
    replay.update(
        {
            "delivery_mode": "pending",
            "manual_reason": None,
            "replay_count": int(event.get("replay_count", 0)) + 1,
            "last_replayed_by": operator,
            "last_replayed_at": datetime.now(timezone.utc).isoformat(),
            "replay_reason": reason,
            "webhook_attempted": False,
        }
    )
    return replay


def replay_manual_message(
    message: Any,
    manual_receiver: Any,
    notification_sender: Any,
    operator: str,
    reason: str,
) -> Dict[str, Any]:
    """Republish a manual event and complete it only after broker acceptance.

    Args:
        message: The Service Bus message containing the manual event.
        manual_receiver: The receiver for the manual queue.
        notification_sender: The sender for the notification queue.
        operator: The identifier of the operator requesting the replay.
        reason: The reason for the replay request.

    Returns: The replayed event dictionary.
    """
    event = json.loads(b"".join(message.body).decode("utf-8"))
    replay = replay_manual_event(event, operator, reason)
    notification_sender.send_messages(
        ServiceBusMessage(
            json.dumps(replay).encode("utf-8"),
            content_type="application/json",
            subject="NotificationReplayRequested",
            message_id=replay["event_id"],
        )
    )
    manual_receiver.complete_message(message)
    return replay
