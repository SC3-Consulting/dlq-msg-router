"""
This module implements the Command Pattern for executing remediation actions on messages in the Dead Letter Queue (DLQ) of an Azure Service Bus (ASB) queue or subscription.
- Each action is encapsulated in a concrete command class that implements the ActionCommand interface.
- The ActionRouter class serves as a factory and dispatcher, mapping string action names to their corresponding command classes and executing them with the appropriate context (message, receiver, sender, etc.).
- The commands support error handling with configurable retries and exponential backoff, ensuring resilience against transient network or broker errors.
- The FixAndRetryCommand includes logic for auto-healing messages with missing fields based on a provided safe defaults map, while the EscalateCommand routes unresolvable anomalies to a parking lot for human review.
- The design allows for easy extension by adding new command classes and updating the ActionRouter's command mapping, promoting maintainability and scalability of the DLQ remediation framework.

N.B. The pipeline is an at-least-once, not exactly-once, delivery system. The commands are designed to be idempotent where possible,
but duplicate messages may occur in the main queue or parking lot due to network failures or broker errors during send/complete operations as a result of the non-atomic nature of these operations.

Local idempotency and classification caches are not currently safe as a cross-replica consistency mechanism.

Completion failures after a successful send can result in duplicate messages in the main queue, which is a known limitation of the current design.
"""

import json
import logging
import os
import re
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Union

from azure.servicebus import (
    ServiceBusMessage,
    ServiceBusReceivedMessage,
    ServiceBusReceiver,
    ServiceBusSender,
)

from src.notification import build_notification_event
from src.resilience import backoff_sleep


class ActionCommand(ABC):
    """
    Abstract base class for all Dead Letter Queue remediation actions.
    Enforces the Command Pattern for decoupled execution behaviour.
    Concrete implementations must define the execute() method to perform the specific action.
    Provides shared utilities for broker operations, message cloning, and correlation context propagation.
    Attributes:
        logger (logging.Logger): Logger instance for logging action execution details.
    Methods:
        _broker_op(operation_name, fn, *args, **kwargs): Executes a broker operation with retries and exponential backoff.
        execute(message, receiver, sender, parking_lot_sender, pattern, safe_defaults_map): Abstract method to be implemented by concrete commands for executing the action.
        _clone_for_resubmit(original, new_body): Clones a Service Bus message for resubmission, preserving correlation context and incrementing retry counters.
        _propagate_correlation_context(safe_properties, original): Propagates correlation context from the original message to the new message's application properties, ensuring traceability across retries and escalations.
    """

    # TODO: The action executor does send then complete for retry/fix_and_retry which is not atomic.
    # If the send succeeds but the complete fails, the message will be duplicated in the main queue.
    # This is a known limitation of the current design and may require a more robust transactional approach
    # or idempotency handling to prevent duplicates in high-throughput scenarios.

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self._action_max_attempts = int(os.getenv("ACTION_RETRY_MAX_ATTEMPTS", "3"))
        self._action_backoff_base = float(
            os.getenv("ACTION_BACKOFF_BASE_SECONDS", "0.5")
        )
        self._action_backoff_max = float(
            os.getenv("ACTION_BACKOFF_MAX_SECONDS", "15.0")
        )

    def _broker_op(self, operation_name: str, fn, *args, **kwargs):
        """
        Execute a broker operation (complete, send, abandon, dead_letter) with
        per-action configurable retries and exponential backoff with jitter.
        Transient errors are retried; the last exception is re-raised on exhaustion.
        Args:
            operation_name (str): A descriptive name for the operation, used in logging.
            fn (callable): The broker operation function to execute (e.g., receiver.complete_message).
            *args: Positional arguments to pass to the broker operation function.
            **kwargs: Keyword arguments to pass to the broker operation function.
        Raises:
            Exception: Re-raises the last exception encountered if all retry attempts are exhausted.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self._action_max_attempts):
            try:
                fn(*args, **kwargs)
                return
            except Exception as e:
                last_exc = e
                if attempt < self._action_max_attempts - 1:
                    sleep_dur = backoff_sleep(
                        attempt,
                        base_seconds=self._action_backoff_base,
                        max_seconds=self._action_backoff_max,
                    )
                    self.logger.warning(
                        f"{operation_name} failed (attempt {attempt + 1}/{self._action_max_attempts}, "
                        f"backoff {sleep_dur:.1f}s): {e}"
                    )
        self.logger.error(
            f"{operation_name} exhausted {self._action_max_attempts} attempts. Last error: {last_exc}",
            exc_info=True,
        )
        if last_exc is None:
            raise RuntimeError(
                f"{operation_name} failed without capturing an exception."
            )
        raise last_exc

    def _complete_message(self, operation_name: str, receiver, message) -> None:
        """Complete a message and propagate failure to the batch safety boundary.

        Args:
            operation_name (str): A descriptive name for the operation, used in logging.
            receiver (ServiceBusReceiver): The receiver instance for the DLQ.
            message (ServiceBusReceivedMessage): The message to complete.
        """
        self._broker_op(operation_name, receiver.complete_message, message)

    @staticmethod
    def _current_resubmit_count(properties: Dict[Union[str, bytes], Any]) -> int:
        """Return a non-negative retry count even when broker properties are malformed.

        Args:
            properties (Dict[Union[str, bytes], Any]): The application properties of the message.
        Returns:
            int: The current resubmit count, defaulting to 0 if not present or malformed.
        """
        raw_count = properties.get("Resubmit-Count", 0)
        try:
            return max(0, int(raw_count))
        except (TypeError, ValueError):
            return 0

    @abstractmethod
    def execute(
        self,
        message: Any,
        receiver: ServiceBusReceiver,
        sender: ServiceBusSender,
        parking_lot_sender: ServiceBusSender,
        pattern: str,
        safe_defaults_map: Optional[dict] = None,
    ) -> None:
        """Executes the specific remediation action on the broker.
        Args:
            message (Any): The Service Bus message to act upon.
            receiver (ServiceBusReceiver): The receiver instance for the DLQ.
            sender (ServiceBusSender): The sender instance for the main queue.
            parking_lot_sender (ServiceBusSender): The sender instance for the parking lot queue.
            pattern (str): The pattern to match for the remediation action.
            safe_defaults_map (Optional[dict]): A map of safe default values for the action.
        """
        pass

    def _clone_for_resubmit(
        self, original: ServiceBusReceivedMessage, new_body: Optional[bytes] = None
    ) -> ServiceBusMessage:
        """
        Safely clones an ASB message to generate a fresh lock token and message ID,
        whilst preserving correlation data and incrementing retry counters.
        Args:
            original (ServiceBusReceivedMessage): The original message to clone.
            new_body (Optional[bytes]): Optional new body for the cloned message. If None, the original body is used.
        Returns:
            ServiceBusMessage: A new message instance ready for resubmission to the main queue or
        """
        body = new_body if new_body else b"".join(original.body)

        # FEATURE FLAG: Allow Ops to control UUID generation vs preserving original ID
        enable_new_id = (
            os.getenv("ENABLE_NEW_MESSAGE_ID_ON_RETRY", "True").lower() == "true"
        )
        new_msg_id = str(uuid.uuid4()) if enable_new_id else original.message_id

        new_msg = ServiceBusMessage(
            body=body,
            content_type=original.content_type,
            subject=original.subject,
            correlation_id=original.correlation_id,
            message_id=new_msg_id,
        )

        safe_properties: Dict[Union[str, bytes], Any] = {}
        if original.application_properties:
            for k, v in original.application_properties.items():
                key_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                val_str = v.decode("utf-8") if isinstance(v, bytes) else str(v)
                safe_properties[key_str] = val_str

        self._propagate_correlation_context(safe_properties, original)

        # Loop Prevention Protocol
        count_key = "Resubmit-Count"
        current_count = self._current_resubmit_count(safe_properties)
        safe_properties[count_key] = current_count + 1
        safe_properties["OriginalDeadLetterReason"] = (
            original.dead_letter_reason or "Unknown"
        )

        new_msg.application_properties = safe_properties
        return new_msg

    def _propagate_correlation_context(
        self,
        safe_properties: Dict[Union[str, bytes], Any],
        original: ServiceBusReceivedMessage,
    ) -> None:
        """Propagates correlation context from the original message to the new message's application properties.
        Ensures traceability across retries and escalations by preserving traceparent, tracestate, diagnostic-id, and correlation identifiers.
        Args:
            safe_properties (Dict[Union[str, bytes], Any]): The application properties of the new message.
            original (ServiceBusReceivedMessage): The original message from which to propagate context.
        """

        def first_key(*keys):
            """Returns the first non-empty value for the given keys from the original message's application properties.
            Args:
                *keys: A variable number of keys to search for in the original message's application properties
            Returns:
                The first non-empty value found for the given keys, or None if none are found.
            """
            for key in keys:
                if key in safe_properties and safe_properties[key]:
                    return str(safe_properties[key])
            return None

        traceparent = first_key(
            "traceparent", "Traceparent", "diagnostic-id", "Diagnostic-Id"
        )
        if traceparent:
            safe_properties.setdefault("traceparent", traceparent)

        tracestate = first_key("tracestate", "Tracestate")
        if tracestate:
            safe_properties.setdefault("tracestate", tracestate)

        diagnostic_id = first_key("diagnostic-id", "Diagnostic-Id")
        if diagnostic_id:
            safe_properties.setdefault("diagnostic-id", diagnostic_id)

        trace_id = first_key("trace_id", "traceid", "otel.trace_id", "x-b3-traceid")
        span_id = first_key("span_id", "spanid", "otel.span_id", "x-b3-spanid")

        if traceparent:
            match = re.match(
                r"^00-([0-9a-fA-F]{32})-([0-9a-fA-F]{16})-[0-9a-fA-F]{2}$", traceparent
            )
            if match:
                trace_id = match.group(1).lower()
                span_id = match.group(2).lower()

        if trace_id:
            safe_properties.setdefault("trace_id", trace_id)
        if span_id:
            safe_properties.setdefault("span_id", span_id)

        correlation_id = original.correlation_id or original.message_id
        if correlation_id:
            safe_properties.setdefault("correlation_id", str(correlation_id))
            safe_properties.setdefault("Correlation-Id", str(correlation_id))

        if original.message_id:
            safe_properties.setdefault("OriginalMessageId", str(original.message_id))


class DropCommand(ActionCommand):
    def execute(
        self,
        message: Any,
        receiver: ServiceBusReceiver,
        sender: ServiceBusSender,
        parking_lot_sender: ServiceBusSender,
        pattern: str,
        safe_defaults_map: Optional[dict] = None,
    ) -> None:
        """
        Executes the drop action by completing the message in the DLQ, effectively removing it from processing.
        Args:
            message (Any): The Service Bus message to drop.
            receiver (ServiceBusReceiver): The receiver instance for the DLQ.
            sender (ServiceBusSender): The sender instance for the main queue.
            parking_lot_sender (ServiceBusSender): The sender instance for the parking lot queue.
            pattern (str): The pattern to match for the drop action.
            safe_defaults_map (Optional[dict]): A dictionary of safe default values for message properties.
        """
        self.logger.info(
            f"Dropping message {message.message_id}. Reason: {message.dead_letter_reason}"
        )
        self._complete_message("complete(drop)", receiver, message)


class DropAndNotifyCommand(ActionCommand):
    """
    Executes a high-severity drop action for messages that require immediate attention.
    This command publishes a durable notification event and then completes the message in the DLQ.
    Attributes:
        logger (logging.Logger): Logger instance for logging action execution details.
    Methods:
        execute(message, receiver, sender, parking_lot_sender, pattern, safe_defaults_map): Executes the drop and notify action on the broker.
    """

    def __init__(self, notification_sender=None, source_queue: Optional[str] = None):
        super().__init__()
        self.notification_sender = notification_sender
        self.source_queue = source_queue

    def execute(
        self,
        message: Any,
        receiver: ServiceBusReceiver,
        sender: ServiceBusSender,
        parking_lot_sender: ServiceBusSender,
        pattern: str,
        safe_defaults_map: Optional[dict] = None,
    ) -> None:
        """
        Executes the drop and notify action by publishing an event before completing the message in the DLQ.
        Args:
            message (Any): The Service Bus message to drop and notify.
            receiver (ServiceBusReceiver): The receiver instance for the DLQ.
            sender (ServiceBusSender): The sender instance for the main queue.
            parking_lot_sender (ServiceBusSender): The sender instance for the parking lot queue.
            pattern (str): The pattern to match for the drop and notify action.
            safe_defaults_map (Optional[dict]): A dictionary of safe default values for message properties.
        """
        if self.notification_sender is None:
            raise RuntimeError(
                "drop_and_notify requires a configured notification sender"
            )

        event = build_notification_event(message, pattern, self.source_queue)
        alert = ServiceBusMessage(
            body=json.dumps(event).encode("utf-8"),
            content_type="application/json",
            subject="DeadLetterMessageDropped",
            message_id=event["event_id"],
            application_properties={
                "event_type": event["event_type"],
                "schema_version": event["schema_version"],
                "client_id": event["client_id"],
            },
        )
        self._broker_op(
            "send(notification)", self.notification_sender.send_messages, alert
        )
        self.logger.info(
            f"Notification event {event['event_id']} published for message {message.message_id}."
        )
        self._broker_op("complete(drop_and_notify)", receiver.complete_message, message)


class RetryCommand(ActionCommand):
    """
    Executes a retry action for messages that can be safely resubmitted to the main queue.
    This command clones the message, sends it to the main queue, and completes the original message in the DLQ.
    Attributes:
        logger (logging.Logger): Logger instance for logging action execution details.
    Methods:
        execute(message, receiver, sender, parking_lot_sender, pattern, safe_defaults_map): Executes the retry action on the broker.
    """

    def execute(
        self,
        message: Any,
        receiver: ServiceBusReceiver,
        sender: ServiceBusSender,
        parking_lot_sender: ServiceBusSender,
        pattern: str,
        safe_defaults_map: Optional[dict] = None,
    ) -> None:
        try:
            new_msg = self._clone_for_resubmit(message)
            self._broker_op("send(retry)", sender.send_messages, new_msg)
            self.logger.info(
                f"Successfully cloned and resubmitted message {message.message_id} to main queue."
            )
            self._complete_message("complete(retry)", receiver, message)

        except Exception as e:
            self.logger.error(
                f"Failed to execute retry for message {message.message_id}: {str(e)}",
                exc_info=True,
            )
            if os.getenv("ENABLE_NESTED_BROKER_EXCEPTIONS", "True").lower() == "true":
                try:
                    self._broker_op(
                        "abandon(retry_fallback)", receiver.abandon_message, message
                    )
                except Exception as abandon_err:
                    self.logger.error(
                        f"Network error abandoning message {message.message_id}: {str(abandon_err)}",
                        exc_info=True,
                    )
            else:
                receiver.abandon_message(message)


class FixAndRetryCommand(ActionCommand):
    """
    Executes a fix and retry action for messages that can be safely resubmitted to the main queue after applying structural fixes.
    This command attempts to fix missing fields in the message payload using safe defaults, clones the message, sends it to the main queue, and completes the original message in the DLQ.
    Attributes:
        logger (logging.Logger): Logger instance for logging action execution details.
    Methods:
        execute(message, receiver, sender, parking_lot_sender, pattern, safe_defaults_map): Executes the fix and retry action on the broker.
    """

    def execute(
        self,
        message: Any,
        receiver: ServiceBusReceiver,
        sender: ServiceBusSender,
        parking_lot_sender: ServiceBusSender,
        pattern: str,
        safe_defaults_map: Optional[dict] = None,
    ) -> None:
        """
        Executes the fix and retry action by attempting to auto-heal the message payload, resubmitting it to the main queue, and completing the original message in the DLQ.
        Args:
            message (Any): The Service Bus message to fix and retry.
            receiver (ServiceBusReceiver): The receiver instance for the DLQ.
            sender (ServiceBusSender): The sender instance for the main queue.
            parking_lot_sender (ServiceBusSender): The sender instance for the parking lot queue.
            pattern (str): The pattern to match for the fix and retry action.
            safe_defaults_map (Optional[dict]): A dictionary of safe default values for message properties.
        """
        try:
            raw_body = b"".join(message.body).decode("utf-8")
            payload_dict = json.loads(raw_body)

            if not isinstance(payload_dict, dict):
                raise ValueError(
                    "Payload is not a dictionary. Cannot perform key-value auto-healing."
                )

            if pattern and pattern.startswith("missing_field_"):
                missing_field = pattern.replace("missing_field_", "")

                if safe_defaults_map and missing_field in safe_defaults_map:
                    safe_value = safe_defaults_map[missing_field]
                    payload_dict[missing_field] = safe_value
                    self.logger.info(
                        f"Structural fix applied: Injected '{missing_field}' with strongly-typed value '{safe_value}' ({type(safe_value).__name__})."
                    )
                else:
                    self.logger.warning(
                        f"No safe default mapped for '{missing_field}'. Downgrading to EscalateCommand."
                    )
                    EscalateCommand().execute(
                        message,
                        receiver,
                        sender,
                        parking_lot_sender,
                        pattern,
                        safe_defaults_map,
                    )
                    return
            else:
                # Catch regex misses to prevent infinite unhealed resubmissions
                self.logger.warning(
                    f"FixAndRetry cannot determine missing field from pattern '{pattern}'. Downgrading to EscalateCommand."
                )
                EscalateCommand().execute(
                    message,
                    receiver,
                    sender,
                    parking_lot_sender,
                    pattern,
                    safe_defaults_map,
                )
                return

            fixed_body_bytes = json.dumps(payload_dict).encode("utf-8")
            new_msg = self._clone_for_resubmit(message, new_body=fixed_body_bytes)
            application_properties = new_msg.application_properties or {}
            application_properties["AutoFixed"] = "True"
            new_msg.application_properties = application_properties

            self._broker_op("send(fix_and_retry)", sender.send_messages, new_msg)
            self.logger.info(
                f"Successfully auto-healed and resubmitted message {message.message_id}."
            )

            self._complete_message("complete(fix_and_retry)", receiver, message)

        except json.JSONDecodeError:
            self.logger.error(
                f"Message {message.message_id} is invalid JSON. Bypassing fix and escalating."
            )
            EscalateCommand().execute(
                message,
                receiver,
                sender,
                parking_lot_sender,
                pattern,
                safe_defaults_map,
            )
        except Exception as e:
            self.logger.error(
                f"Unexpected error during auto-heal for {message.message_id}: {str(e)}",
                exc_info=True,
            )
            try:
                self._broker_op(
                    "abandon(fix_and_retry_fallback)", receiver.abandon_message, message
                )
            except Exception as abandon_err:
                self.logger.error(
                    f"Network error abandoning message {message.message_id} after fix failure: {str(abandon_err)}",
                    exc_info=True,
                )


class EscalateCommand(ActionCommand):
    """
    Executes an escalate action for messages that require human review.
    This command clones the message, sends it to the parking lot queue, and completes the original message in the DLQ.
    Attributes:
        logger (logging.Logger): Logger instance for logging action execution details.
    Methods:
        execute(message, receiver, sender, parking_lot_sender, pattern, safe_defaults_map): Executes the escalate action on the broker.
    """

    def execute(
        self,
        message: Any,
        receiver: ServiceBusReceiver,
        sender: ServiceBusSender,
        parking_lot_sender: ServiceBusSender,
        pattern: str,
        safe_defaults_map: Optional[dict] = None,
    ) -> None:
        """
        Executes the escalate action by routing the message to a parking lot queue for human review, and completing the original message in the DLQ.
        Args:
            message (Any): The Service Bus message to escalate.
            receiver (ServiceBusReceiver): The receiver instance for the DLQ.
            sender (ServiceBusSender): The sender instance for the main queue.
            parking_lot_sender (ServiceBusSender): The sender instance for the parking lot queue.
            pattern (str): The pattern to match for the escalate action.
            safe_defaults_map (Optional[dict]): A dictionary of safe default values for message properties.
        """
        try:
            self.logger.warning(
                f"[TICKET CREATED] Escalating anomaly {message.message_id} for human review."
            )
            new_msg = self._clone_for_resubmit(message)
            self._broker_op("send(escalate)", parking_lot_sender.send_messages, new_msg)

            self._complete_message("complete(escalate)", receiver, message)

        except Exception as e:
            self.logger.error(
                f"Failed to route message {message.message_id} to parking lot: {str(e)}",
                exc_info=True,
            )
            if os.getenv("ENABLE_NESTED_BROKER_EXCEPTIONS", "True").lower() == "true":
                try:
                    self._broker_op(
                        "abandon(escalate_fallback)", receiver.abandon_message, message
                    )
                except Exception as abandon_err:
                    self.logger.error(
                        f"Network error abandoning message {message.message_id} during escalation failure: {str(abandon_err)}",
                        exc_info=True,
                    )
            else:
                receiver.abandon_message(message)


class ActionRouter:
    """Factory mapping string actions to physical execution commands.
    Attributes:
        receiver (ServiceBusReceiver): The receiver instance for the DLQ.
        sender (ServiceBusSender): The sender instance for the main queue.
        parking_lot_sender (ServiceBusSender): The sender instance for the parking lot queue.
        _commands (dict): A mapping of action names to their corresponding command instances.
    Methods:
        route_and_execute(action_name, message, pattern, safe_defaults_map): Routes the action name to the appropriate command and executes it with the provided context.
    """

    def __init__(
        self,
        receiver: ServiceBusReceiver,
        sender: ServiceBusSender,
        parking_lot_sender: ServiceBusSender,
        notification_sender: Optional[ServiceBusSender] = None,
        source_queue: Optional[str] = None,
    ):
        """
        Initialises the ActionRouter with the provided Service Bus receiver and sender instances.
        Args:
            receiver (ServiceBusReceiver): The receiver instance for the DLQ.
            sender (ServiceBusSender): The sender instance for the main queue.
            parking_lot_sender (ServiceBusSender): The sender instance for the parking lot queue.
        """
        self.receiver = receiver
        self.sender = sender
        self.parking_lot_sender = parking_lot_sender
        self.notification_sender = notification_sender
        self.source_queue = source_queue

        # TODO: The current command mapping is static and hardcoded. For future extensibility (far future capability),
        # consider implementing a dynamic plugin system or configuration-driven approach to allow new commands to be registered without modifying the core codebase.
        # This could involve loading command classes from a specified directory or using entry points in a package distribution.

        # TODO: drop, retry, and escalate are operations with side effects and should have their own idempotency implementations.
        # The destination queues or parking lot should also tolerate duplicate inserts because the current design does not guarantee atomicity between send and complete.

        self._commands = {
            "drop": DropCommand(),
            "drop_and_notify": DropAndNotifyCommand(
                notification_sender=notification_sender, source_queue=source_queue
            ),
            "retry": RetryCommand(),
            "fix_and_retry": FixAndRetryCommand(),
            "escalate": EscalateCommand(),
        }
        self.logger = logging.getLogger("ActionRouter")

    def route_and_execute(
        self,
        action_name: str,
        message: Any,
        pattern: str,
        safe_defaults_map: Optional[dict] = None,
    ) -> None:
        """
        Routes the provided action name to the corresponding command and executes it with the given message and context.
        Args:
            action_name (str): The name of the action to execute (e.g., "drop", "retry", "fix_and_retry", "escalate").
            message (Any): The Service Bus message to act upon.
            pattern (str): The pattern to match for the action.
            safe_defaults_map (Optional[dict]): A dictionary of safe default values for message properties.
        """
        safe_action = action_name or "escalate"
        command = self._commands.get(safe_action.lower())

        if not command:
            self.logger.warning(
                f"Unrecognised action '{safe_action}'. Defaulting to escalate."
            )
            command = self._commands["escalate"]

        command.execute(
            message,
            self.receiver,
            self.sender,
            self.parking_lot_sender,
            pattern,
            safe_defaults_map,
        )
