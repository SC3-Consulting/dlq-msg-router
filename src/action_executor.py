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
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str, safe_defaults_map: dict = None) -> None:
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
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str, safe_defaults_map: dict = None) -> None:
        self.logger.info(f"Dropping message {message.message_id}. Reason: {message.dead_letter_reason}")
        try:
            receiver.complete_message(message)
        except Exception as e:
            self.logger.error(f"Network error completing dropped message {message.message_id}: {str(e)}", exc_info=True)


class DropAndNotifyCommand(ActionCommand):
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str, safe_defaults_map: dict = None) -> None:
        self.logger.warning(f"[ALERT DISPATCHED] High severity drop executed for message {message.message_id}.")
        try:
            receiver.complete_message(message)
        except Exception as e:
            self.logger.error(f"Network error completing drop_and_notify message {message.message_id}: {str(e)}", exc_info=True)


class RetryCommand(ActionCommand):
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str, safe_defaults_map: dict = None) -> None:
        try:
            new_msg = self._clone_for_resubmit(message)
            sender.send_messages(new_msg)
            self.logger.info(f"Successfully cloned and resubmitted message {message.message_id} to main queue.")
            try:
                receiver.complete_message(message)
            except Exception as complete_err:
                self.logger.error(f"Network error completing original message {message.message_id} after resubmit: {str(complete_err)}", exc_info=True)

        except Exception as e:
            self.logger.error(f"Failed to execute retry for message {message.message_id}: {str(e)}", exc_info=True)
            # NESTED CATCH: Prevent cascading crashes if Azure is totally offline
            # FEATURE FLAG: Toggle nested exception safety based on Ops preference
            if os.getenv("ENABLE_NESTED_BROKER_EXCEPTIONS", "True").lower() == "true":
                try:
                    receiver.abandon_message(message)
                except Exception as abandon_err:
                    self.logger.error(f"Network error abandoning message {message.message_id}: {str(abandon_err)}", exc_info=True)
            else:
                # Terminal exception bubble up for container restart policy
                receiver.abandon_message(message)


class FixAndRetryCommand(ActionCommand):
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str, safe_defaults_map: dict = None) -> None:
        try:
            raw_body = b"".join(message.body).decode('utf-8')
            payload_dict = json.loads(raw_body)

            if not isinstance(payload_dict, dict):
                raise ValueError("Payload is not a dictionary. Cannot perform key-value auto-healing.")

            if pattern and pattern.startswith("missing_field_"):
                missing_field = pattern.replace("missing_field_", "")
                
                if safe_defaults_map and missing_field in safe_defaults_map:
                    safe_value = safe_defaults_map[missing_field]
                    payload_dict[missing_field] = safe_value
                    self.logger.info(f"Structural fix applied: Injected '{missing_field}' with strongly-typed value '{safe_value}' ({type(safe_value).__name__}).")
                else:
                    self.logger.warning(f"No safe default mapped for '{missing_field}'. Downgrading to EscalateCommand.")
                    EscalateCommand().execute(message, receiver, sender, parking_lot_sender, pattern, safe_defaults_map)
                    return 
            else:
                # Catch regex misses to prevent infinite unhealed resubmissions
                self.logger.warning(f"FixAndRetry cannot determine missing field from pattern '{pattern}'. Downgrading to EscalateCommand.")
                EscalateCommand().execute(message, receiver, sender, parking_lot_sender, pattern, safe_defaults_map)
                return

            fixed_body_bytes = json.dumps(payload_dict).encode('utf-8')
            new_msg = self._clone_for_resubmit(message, new_body=fixed_body_bytes)
            new_msg.application_properties["AutoFixed"] = "True"

            sender.send_messages(new_msg)
            self.logger.info(f"Successfully auto-healed and resubmitted message {message.message_id}.")
            
            try:
                receiver.complete_message(message)
            except Exception as complete_err:
                self.logger.error(f"Network error completing original message {message.message_id} after auto-heal: {str(complete_err)}", exc_info=True)

        except json.JSONDecodeError:
            self.logger.error(f"Message {message.message_id} is invalid JSON. Bypassing fix and escalating.")
            EscalateCommand().execute(message, receiver, sender, parking_lot_sender, pattern, safe_defaults_map)
        except Exception as e:
            self.logger.error(f"Unexpected error during auto-heal for {message.message_id}: {str(e)}", exc_info=True)
            try:
                receiver.abandon_message(message)
            except Exception as abandon_err:
                self.logger.error(f"Network error abandoning message {message.message_id} after fix failure: {str(abandon_err)}", exc_info=True)


class EscalateCommand(ActionCommand):
    def execute(self, message: ServiceBusReceivedMessage, receiver: ServiceBusReceiver, sender: ServiceBusSender, parking_lot_sender: ServiceBusSender, pattern: str, safe_defaults_map: dict = None) -> None:
        try:
            self.logger.warning(f"[TICKET CREATED] Escalating anomaly {message.message_id} for human review.")
            new_msg = self._clone_for_resubmit(message)
            parking_lot_sender.send_messages(new_msg)
            
            try:
                receiver.complete_message(message)
            except Exception as complete_err:
                self.logger.error(f"Network error completing message {message.message_id} after escalation: {str(complete_err)}", exc_info=True)
                
        except Exception as e:
            self.logger.error(f"Failed to route message {message.message_id} to parking lot: {str(e)}", exc_info=True)
            if os.getenv("ENABLE_NESTED_BROKER_EXCEPTIONS", "True").lower() == "true":
                try:
                    receiver.abandon_message(message)
                except Exception as abandon_err:
                    self.logger.error(f"Network error abandoning message {message.message_id} during escalation failure: {str(abandon_err)}", exc_info=True)
            else:
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

    def route_and_execute(self, action_name: str, message: ServiceBusReceivedMessage, pattern: str, safe_defaults_map: dict = None) -> None:
        safe_action = action_name or "escalate"
        command = self._commands.get(safe_action.lower())
        
        if not command:
            self.logger.warning(f"Unrecognised action '{safe_action}'. Defaulting to escalate.")
            command = self._commands["escalate"]
            
        command.execute(message, self.receiver, self.sender, self.parking_lot_sender, pattern, safe_defaults_map)