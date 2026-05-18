import json
import logging
import os
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage

# Set up logging to see clean terminal output
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# CONFIGURATION - Replace with your exact namespace name
FULLY_QUALIFIED_NAMESPACE = "viva-sb-ns-swastik.servicebus.windows.net"
QUEUE_NAME = "viva-integration-queue"

def generate_payloads():
    """Generates the three distinct architectural testing payloads."""
    
    happy_path = {
        "metadata": {
            "eventId": "evt-2026-0518-001",
            "correlationId": "corr-sf-sap-9901",
            "sourceSystem": "Salesforce",
            "eventType": "OrderCreated",
            "timestamp": "2026-05-18T03:00:00Z"
        },
        "payload": {
            "orderId": "ORD-77321",
            "clientId": "CLIENT_ACME_CORP",
            "transactionDate": "2026-05-18",
            "amount": 15400.50,
            "currency": "AUD",
            "items": [{"sku": "SKU-VIVA-091", "quantity": 2, "unitPrice": 7700.25}]
        }
    }

    heuristic_catch = {
        "metadata": {
            "eventId": "evt-2026-0518-002",
            "correlationId": "corr-sf-sap-9902",
            "sourceSystem": "Salesforce",
            "eventType": "OrderCreated",
            "timestamp": "2026-05-18T03:01:15Z"
        },
        "payload": {
            "orderId": "ORD-77322",
            "clientId": "CLIENT_ACME_CORP",
            "amount": 2200.00,
            "currency": "AUD",
            "items": [{"sku": "SKU-VIVA-104", "quantity": 1, "unitPrice": 2200.00}]
        }
    }

    ai_catch = {
        "metadata": {
            "eventId": "evt-2026-0518-003",
            "correlationId": "corr-sf-sap-9903",
            "sourceSystem": "Salesforce",
            "eventType": "OrderCreated",
            "timestamp": "2026-05-18T03:02:45Z"
        },
        "payload": {
            "orderId": "ORD-77323",
            "clientId": "TRIGGER_SAP_CRASH",
            "transactionDate": "2026-05-18",
            "amount": 890.00,
            "currency": "AUD",
            "items": [{"sku": "SKU-VIVA-302", "quantity": 5, "unitPrice": 178.00}]
        }
    }

    # 4. The AI Catch DUPLICATE (To test our Token-Saving Cache)
    ai_catch_duplicate = {
        "metadata": {
            "eventId": "evt-2026-0518-004", # New Event ID
            "correlationId": "corr-sf-sap-9904",
            "sourceSystem": "Salesforce",
            "eventType": "OrderCreated",
            "timestamp": "2026-05-18T03:03:10Z" # 25 seconds later
        },
        "payload": {
            "orderId": "ORD-77324", # Different Order
            "clientId": "TRIGGER_SAP_CRASH", # EXACT SAME CRASH TRIGGER
            "transactionDate": "2026-05-18",
            "amount": 1200.00,
            "currency": "AUD",
            "items": [{"sku": "SKU-VIVA-302", "quantity": 1, "unitPrice": 1200.00}]
        }
    }

    return [
        ("HAPPY_PATH", happy_path),
        ("HEURISTIC_CATCH", heuristic_catch),
        ("AI_CATCH", ai_catch),
        ("AI_CATCH_DUPLICATE", ai_catch_duplicate)
    ]

def send_messages():
    logger.info("Initializing Zero-Trust Identity Credential...")
    # This automatically picks up your 'az login' session from WSL
    credential = DefaultAzureCredential()

    logger.info(f"Connecting to Azure Service Bus Namespace: {FULLY_QUALIFIED_NAMESPACE}")
    
    # Initialize the client without passwords or connection strings
    try:
        with ServiceBusClient(FULLY_QUALIFIED_NAMESPACE, credential) as client:
            with client.get_queue_sender(queue_name=QUEUE_NAME) as sender:
                
                messages_to_send = generate_payloads()
                
                for label, payload_dict in messages_to_send:
                    # Convert dict to JSON string
                    json_body = json.dumps(payload_dict)
                    
                    # Wrap it in an official Azure Service Bus Message object
                    message = ServiceBusMessage(json_body)
                    
                    logger.info(f"Dispatching test message type: [{label}]")
                    sender.send_messages(message)
                    logger.info(f"Successfully sent [{label}] to {QUEUE_NAME}")
                    
    except Exception as e:
        logger.error(f"Failed to transmit messages: {str(e)}")
        logger.error("Note: This is expected if your Azure RBAC permissions haven't cleared yet.")

if __name__ == "__main__":
    send_messages()