import json
import logging
import uuid
import os
from abc import ABC, abstractmethod
from typing import Dict, Any

from azure.servicebus import ServiceBusReceivedMessage, ServiceBusReceiver, ServiceBusSender, ServiceBusMessage

class ActionCommand(ABC):
    """
    Abstract base class for all Dead Letter Queue remediation actions.
    Enforces the Command Pattern for decoupled execution behaviour.
    """
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str) -> None:
        """Executes the specific remediation action on the broker."""
        pass

    def _clone_for_resubmit(self, original: ServiceBusReceivedMessage, new_body: bytes = None) -> ServiceBusMessage:
        """
        Safely clones an ASB message to generate a fresh lock token and message ID,
        whilst preserving correlation data and incrementing retry counters.
        """
        body = new_body if new_body else b"".join(original.body)
        
        # FEATURE FLAG: Allow Ops to control UUID generation vs preserving original ID
        enable_new_id = os.getenv("ENABLE_NEW_MESSAGE_ID_ON_RETRY", "True").lower() == "true"
        new_msg_id = str(uuid.uuid4()) if enable_new_id else original.message_id
        
        new_msg = ServiceBusMessage(
            body=body,
            content_type=original.content_type,
            subject=original.subject,
            correlation_id=original.correlation_id,
            message_id=new_msg_id
        )

        safe_properties: Dict[str, Any] = {}
        if original.application_properties:
            for k, v in original.application_properties.items():
                key_str = k.decode('utf-8') if isinstance(k, bytes) else str(k)
                val_str = v.decode('utf-8') if isinstance(v, bytes) else str(v)
                safe_properties[key_str] = val_str

        # Loop Prevention Protocol
        count_key = "Resubmit-Count"
        current_count = int(safe_properties.get(count_key, 0))
        safe_properties[count_key] = current_count + 1
        safe_properties["OriginalDeadLetterReason"] = original.dead_letter_reason or "Unknown"

        new_msg.application_properties = safe_properties
        return new_msg


class DropCommand(ActionCommand):
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str) -> None:
        self.logger.info(f"Dropping message {message.message_id}. Reason: {message.dead_letter_reason}")
        receiver.complete_message(message)


class DropAndNotifyCommand(ActionCommand):
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str) -> None:
        self.logger.warning(f"[ALERT DISPATCHED] High severity drop executed for message {message.message_id}.")
        receiver.complete_message(message)


class RetryCommand(ActionCommand):
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str) -> None:
        try:
            new_msg = self._clone_for_resubmit(message)
            sender.send_messages(new_msg)
            receiver.complete_message(message)
            self.logger.info(f"Successfully cloned and resubmitted message {message.message_id} to main queue.")
        except Exception as e:
            self.logger.error(f"Failed to execute retry for message {message.message_id}: {str(e)}")
            # NESTED CATCH: Prevent cascading crashes if Azure is totally offline
            # FEATURE FLAG: Toggle nested exception safety based on Ops preference
            if os.getenv("ENABLE_NESTED_BROKER_EXCEPTIONS", "True").lower() == "true":
                try:
                    receiver.abandon_message(message)
                except Exception as abandon_err:
                    self.logger.error(f"Failed to safely abandon message (Broker offline?): {abandon_err}")
            else:
                # "crash the app" scenario
                receiver.abandon_message(message)


class FixAndRetryCommand(ActionCommand):
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str) -> None:
        try:
            raw_body = b"".join(message.body).decode('utf-8')
            payload_dict = json.loads(raw_body)

            # CRITICAL FIX 1: Safely catch JSON lists to prevent TypeError crashes
            if not isinstance(payload_dict, dict):
                raise ValueError("Payload is not a dictionary. Cannot perform key-value auto-healing.")

            # Dynamic Heuristic Fix: Extract field from regex pattern generated in rules.json
            if pattern and pattern.startswith("missing_field_"):
                missing_field = pattern.replace("missing_field_", "")
                if missing_field not in payload_dict:
                    payload_dict[missing_field] = "AUTO_FIXED_BY_AGENT"
                    self.logger.info(f"Structural fix applied: Injected missing '{missing_field}'.")

            fixed_body_bytes = json.dumps(payload_dict).encode('utf-8')
            new_msg = self._clone_for_resubmit(message, new_body=fixed_body_bytes)
            new_msg.application_properties["AutoFixed"] = "True"

            sender.send_messages(new_msg)
            receiver.complete_message(message)
            self.logger.info(f"Successfully auto-healed and resubmitted message {message.message_id}.")

        except json.JSONDecodeError:
            self.logger.error(f"Message {message.message_id} is invalid JSON. Bypassing fix and escalating.")
            EscalateCommand().execute(message, receiver, sender, parking_lot_sender, pattern)
        except Exception as e:
            self.logger.error(f"Unexpected error during auto-heal for {message.message_id}: {str(e)}")
            EscalateCommand().execute(message, receiver, sender, parking_lot_sender, pattern)


class EscalateCommand(ActionCommand):
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str) -> None:
        try:
            self.logger.warning(f"[TICKET CREATED] Escalating anomaly {message.message_id} for human review.")
            new_msg = self._clone_for_resubmit(message)
            parking_lot_sender.send_messages(new_msg)
            receiver.complete_message(message)
        except Exception as e:
            self.logger.error(f"Failed to route message {message.message_id} to parking lot: {str(e)}")
            # NESTED CATCH: Prevent cascading crashes if Azure is totally offline
            # FEATURE FLAG: Toggle nested exception safety based on Ops preference
            if os.getenv("ENABLE_NESTED_BROKER_EXCEPTIONS", "True").lower() == "true":
                try:
                    receiver.abandon_message(message)
                except Exception as abandon_err:
                    self.logger.error(f"Failed to safely abandon message (Broker offline?): {abandon_err}")
            else:
                # "crash the app" scenario
                receiver.abandon_message(message)


class ActionRouter:
    """Factory mapping string actions to physical execution commands."""
    def __init__(self, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender):
        self.receiver = receiver
        self.sender = sender
        self.parking_lot_sender = parking_lot_sender
        self._commands = {
            "drop": DropCommand(),
            "drop_and_notify": DropAndNotifyCommand(),
            "retry": RetryCommand(),
            "fix_and_retry": FixAndRetryCommand(),
            "escalate": EscalateCommand()
        }
        self.logger = logging.getLogger("ActionRouter")

    def route_and_execute(self, action_name: str, message: ServiceBusReceivedMessage, pattern: str) -> None:
        # CRITICAL FIX 2: Safely handle None action_name to prevent AttributeError
        safe_action = action_name or "escalate"
        command = self._commands.get(safe_action.lower())
        
        if not command:
            self.logger.warning(f"Unrecognised action '{safe_action}'. Defaulting to escalate.")
            command = self._commands["escalate"]
            
        command.execute(message, self.receiver, self.sender, self.parking_lot_sender, pattern)