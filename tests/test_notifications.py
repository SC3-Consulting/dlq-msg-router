"""
Tests for the notification worker and related functionality.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import run_notifications
from src.action_executor import ActionRouter, DropCommand
from src.ai_client import BaseAIEngine
from src.autonomous_dlq_classifier import AutonomousDLQClassifier
from src.notification import (
    AppConfigurationRegistryProvider,
    NotificationWorker,
    PermanentWebhookError,
    WebhookConfig,
    WebhookRegistry,
    build_notification_event,
    replay_manual_event,
    replay_manual_message,
)


def received_message(body, properties=None):
    """Creates a mock Service Bus message with the given body and properties for testing purposes."""
    return SimpleNamespace(
        body=[json.dumps(body).encode("utf-8")],
        application_properties=properties or {"client_id": "Client_A"},
        message_id="source-123",
        correlation_id="corr-123",
        dead_letter_reason="MessageSizeExceeded",
        dead_letter_error_description="broker limit",
        content_type="application/json",
        subject="test",
    )


def test_drop_and_notify_publishes_before_completing():
    """Proves that the drop_and_notify action publishes the notification event before completing the original message."""
    receiver = MagicMock()
    notification_sender = MagicMock()
    router = ActionRouter(
        receiver,
        MagicMock(),
        MagicMock(),
        notification_sender=notification_sender,
        source_queue="orders",
    )
    message = received_message({}, {"client_id": "Client_A"})
    events = []
    notification_sender.send_messages.side_effect = lambda alert: events.append(alert)
    receiver.complete_message.side_effect = lambda _: events.append("complete")

    router.route_and_execute(
        "drop_and_notify", message, "payload_exceeds_broker_limits"
    )

    assert events[0] != "complete"
    assert events[1] == "complete"
    event = json.loads(b"".join(events[0].body))
    assert event["source_queue"] == "orders"
    assert event["webhook_attempted"] is False
    assert "secret" not in json.dumps(event)


def test_drop_and_notify_does_not_complete_when_publish_fails():
    """Proves that the drop_and_notify action does not complete the original message if publishing fails."""
    receiver = MagicMock()
    sender = MagicMock()
    sender.send_messages.side_effect = RuntimeError("broker unavailable")
    router = ActionRouter(
        receiver, MagicMock(), MagicMock(), notification_sender=sender
    )

    with pytest.raises(RuntimeError):
        router.route_and_execute(
            "drop_and_notify", received_message({}), "terminal_failure"
        )

    receiver.complete_message.assert_not_called()


def test_registry_missing_mapping_goes_to_manual():
    """Proves that a missing webhook mapping sends the event to manual processing."""
    receiver = MagicMock()
    manual_sender = MagicMock()
    worker = NotificationWorker(receiver, manual_sender, WebhookRegistry("{}"))
    message = received_message({"event_id": "event-1", "client_id": "Client_A"})

    assert worker.process(message) == "manual"
    receiver.complete_message.assert_called_once_with(message)
    manual = json.loads(b"".join(manual_sender.send_messages.call_args[0][0].body))
    assert manual["manual_reason"] == "no_webhook_mapping"
    assert manual["webhook_configured"] is False
    assert manual["webhook_attempted"] is False


def test_webhook_4xx_goes_directly_to_manual_with_attempt_indicator():
    """Proves that a 4xx webhook failure sends the event directly to manual processing with an attempt indicator."""
    receiver = MagicMock()
    manual_sender = MagicMock()
    registry = WebhookRegistry(
        '{"Client_A":{"endpoint":"https://client.example/hook","secret":"test","version":"v2"}}'
    )
    worker = NotificationWorker(receiver, manual_sender, registry)
    message = received_message({"event_id": "event-1", "client_id": "Client_A"})

    with patch("src.notification.requests.post") as post:
        post.return_value.status_code = 403
        assert worker.process(message) == "manual"

    manual = json.loads(b"".join(manual_sender.send_messages.call_args[0][0].body))
    assert manual["manual_reason"] == "webhook_4xx_permanent_failure"
    assert manual["webhook_attempted"] is True
    assert manual["webhook_http_status"] == 403
    receiver.complete_message.assert_called_once_with(message)


def test_transient_webhook_failure_leaves_event_for_redelivery():
    """Proves that a transient webhook failure leaves the event for redelivery."""
    receiver = MagicMock()
    manual_sender = MagicMock()
    registry = WebhookRegistry(
        '{"Client_A":{"endpoint":"https://client.example/hook","secret":"test"}}'
    )
    worker = NotificationWorker(receiver, manual_sender, registry)
    message = received_message({"event_id": "event-1", "client_id": "Client_A"})

    with patch("src.notification.requests.post") as post:
        post.return_value.status_code = 503
        with pytest.raises(Exception):
            worker.process(message)

    receiver.complete_message.assert_not_called()
    manual_sender.send_messages.assert_not_called()


def test_manual_event_replay_preserves_identity_and_audits_operator():
    """Proves that replaying a manual event preserves its identity and audits the operator."""
    event = {"event_id": "event-1", "delivery_mode": "manual", "replay_count": 0}
    replay = replay_manual_event(
        event, "operator@example.com", "Client restored endpoint"
    )
    assert replay["event_id"] == "event-1"
    assert replay["delivery_mode"] == "pending"
    assert replay["replay_count"] == 1
    assert replay["last_replayed_by"] == "operator@example.com"


def test_manual_message_replay_publishes_before_completion():
    """Proves that replaying a manual message publishes the notification event before completing the original message."""
    manual_receiver = MagicMock()
    notification_sender = MagicMock()
    order = []
    notification_sender.send_messages.side_effect = lambda _: order.append("publish")
    manual_receiver.complete_message.side_effect = lambda _: order.append("complete")
    message = received_message(
        {
            "event_id": "event-1",
            "client_id": "Client_A",
            "delivery_mode": "manual",
            "replay_count": 0,
        }
    )

    replay_manual_message(
        message,
        manual_receiver,
        notification_sender,
        "operator@example.com",
        "service restored",
    )

    assert order == ["publish", "complete"]


def test_drop_completion_failure_is_not_reported_as_success():
    """Proves that a failure to complete a dropped message is not reported as a successful drop."""
    receiver = MagicMock()
    receiver.complete_message.side_effect = RuntimeError("lock lost")

    with pytest.raises(RuntimeError, match="lock lost"):
        DropCommand().execute(
            received_message({}), receiver, MagicMock(), MagicMock(), "drop"
        )


def test_malformed_resubmit_count_is_reset_safely():
    """Proves that a malformed resubmit count is reset to 1 when resubmitting a message."""
    from src.action_executor import RetryCommand

    message = received_message({}, {"Resubmit-Count": "not-a-number"})
    sender = MagicMock()
    receiver = MagicMock()
    RetryCommand().execute(message, receiver, sender, MagicMock(), "retry")

    sent = sender.send_messages.call_args[0][0]
    assert sent.application_properties["Resubmit-Count"] == 1


def test_app_configuration_registry_resolves_key_vault_secret():
    """Proves that the AppConfigurationRegistryProvider resolves a Key Vault secret when retrieving a webhook configuration."""
    configuration = MagicMock()
    configuration.get_configuration_setting.return_value = SimpleNamespace(
        value=json.dumps(
            {
                "Client_A": {
                    "endpoint": "https://client.example/hook",
                    "secret_name": "client-a-hmac",
                    "version": "v3",
                }
            }
        ),
        etag="etag-1",
    )
    secret_client = MagicMock()
    secret_client.get_secret.return_value = SimpleNamespace(value="resolved-secret")

    registry = AppConfigurationRegistryProvider(
        configuration, secret_client, label="dev"
    )
    config = registry.get("Client_A")

    assert config.endpoint == "https://client.example/hook"
    assert config.secret == "resolved-secret"
    assert config.registry_version == "v3"
    configuration.get_configuration_setting.assert_called_once_with(
        key="webhook-registry", label="dev"
    )
    secret_client.get_secret.assert_called_once_with("client-a-hmac")


class SalvageTestEngine(BaseAIEngine):
    """A test AI engine that returns a fixed result for testing the salvage_json method."""

    def call_llm(self, client_id, reason, description, payload):
        return {}


def test_salvage_json_uses_fenced_content():
    """Proves that salvage_json extracts JSON content from fenced code blocks."""
    engine = SalvageTestEngine()
    assert engine._salvage_json(
        'prefix\n```json\n{"suggested_action":"escalate"}\n```'
    ) == {"suggested_action": "escalate"}


def test_ai_result_validation_rejects_unknown_actions_and_bad_confidence():
    """Proves that the AI result validation rejects unknown actions and confidence scores outside the range [0, 1]."""
    classifier = object.__new__(AutonomousDLQClassifier)
    assert AutonomousDLQClassifier._is_valid_ai_result(
        classifier,
        {
            "suggested_classification": "Business_Logic_Violation",
            "suggested_pattern": "invalid_state",
            "suggested_action": "escalate",
            "confidence_score": 0.9,
        },
    )
    assert not AutonomousDLQClassifier._is_valid_ai_result(
        classifier,
        {
            "suggested_classification": "Business_Logic_Violation",
            "suggested_pattern": "invalid_state",
            "suggested_action": "custom_delete",
            "confidence_score": 0.9,
        },
    )
    assert not AutonomousDLQClassifier._is_valid_ai_result(
        classifier,
        {
            "suggested_classification": "Business_Logic_Violation",
            "suggested_pattern": "invalid_state",
            "suggested_action": "escalate",
            "confidence_score": 1.5,
        },
    )


def _notification_worker_mocks(monkeypatch, messages, worker_side_effect=None):
    """Sets up mocks for the notification worker tests, including environment variables, Service Bus client, receiver, sender, and worker."""
    monkeypatch.setenv(
        "SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", "notifications.servicebus.windows.net"
    )
    monkeypatch.setenv("NOTIFICATION_RUN_ONCE", "true")
    monkeypatch.setenv("NOTIFICATION_QUEUE_NAME", "notification-queue")
    monkeypatch.setenv("NOTIFICATION_MANUAL_QUEUE_NAME", "notification-manual-queue")

    receiver = MagicMock()
    receiver.__enter__.return_value = receiver
    receiver.__exit__.return_value = False
    receiver.receive_messages.side_effect = messages
    manual_sender = MagicMock()
    manual_sender.__enter__.return_value = manual_sender
    manual_sender.__exit__.return_value = False
    client = MagicMock()
    client.get_queue_receiver.return_value = receiver
    client.get_queue_sender.return_value = manual_sender

    worker = MagicMock()
    if worker_side_effect is not None:
        worker.process.side_effect = worker_side_effect

    monkeypatch.setattr(run_notifications, "DefaultAzureCredential", MagicMock)
    monkeypatch.setattr(
        run_notifications.ServiceBusClientFactory,
        "get_client",
        MagicMock(return_value=client),
    )
    monkeypatch.setattr(
        run_notifications, "NotificationWorker", MagicMock(return_value=worker)
    )
    monkeypatch.setattr(run_notifications, "WebhookRegistry", MagicMock)
    monkeypatch.setattr(run_notifications, "load_dotenv", MagicMock())
    return client, receiver, manual_sender, worker


def test_notification_main_exits_once_when_queue_is_empty(monkeypatch):
    """Proves that the notification main function exits after one iteration when the queue is empty and NOTIFICATION_RUN_ONCE is set to true."""
    client, receiver, manual_sender, worker = _notification_worker_mocks(
        monkeypatch, [[]]
    )

    run_notifications.main()

    client.get_queue_receiver.assert_called_once_with(
        queue_name="notification-queue",
        receive_mode=run_notifications.ServiceBusReceiveMode.PEEK_LOCK,
        prefetch_count=10,
    )
    client.get_queue_sender.assert_called_once_with(
        queue_name="notification-manual-queue"
    )
    receiver.receive_messages.assert_called_once()
    worker.process.assert_not_called()
    manual_sender.__exit__.assert_called_once()


def test_notification_main_processes_messages(monkeypatch):
    """Proves that the notification main function processes messages from the queue."""
    message = MagicMock()
    _, receiver, _, worker = _notification_worker_mocks(monkeypatch, [[message], []])

    run_notifications.main()

    worker.process.assert_called_once_with(message)
    assert receiver.receive_messages.call_count == 2


def test_notification_main_requires_service_bus_namespace(monkeypatch):
    """Proves that the notification main function raises a RuntimeError when no Service Bus namespace or connection string is provided."""
    monkeypatch.delenv("SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE", raising=False)
    monkeypatch.delenv("SERVICE_BUS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(run_notifications, "load_dotenv", MagicMock())

    with pytest.raises(RuntimeError, match="Service Bus namespace"):
        run_notifications.main()


def test_notification_main_keeps_polling_after_processing_failure(monkeypatch):
    """Proves that the notification main function keeps polling the queue after a processing failure."""
    message = MagicMock()
    _, receiver, _, worker = _notification_worker_mocks(
        monkeypatch, [[message], []], RuntimeError("webhook unavailable")
    )

    run_notifications.main()

    worker.process.assert_called_once_with(message)
    assert receiver.receive_messages.call_count == 2
