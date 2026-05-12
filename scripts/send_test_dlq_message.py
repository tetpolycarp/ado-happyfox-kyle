"""
Send a malformed message to a Service Bus queue to test DLQ behavior.

Usage:
    python scripts/send_test_dlq_message.py

Requires the SERVICE_BUS_CONNECTION_STRING env var or a .env file.
"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from azure.servicebus import ServiceBusClient, ServiceBusMessage

CONN_STR = os.environ.get("SERVICE_BUS_CONNECTION_STRING")
QUEUE_NAME = os.environ.get("ADO_CHILD_EVENTS_QUEUE", "ado-child-events")

if not CONN_STR:
    print("ERROR: Set SERVICE_BUS_CONNECTION_STRING env var or add it to .env")
    sys.exit(1)

body = '{"invalid": "this message will fail parsing and eventually land in the DLQ"}'

with ServiceBusClient.from_connection_string(CONN_STR) as client:
    with client.get_queue_sender(QUEUE_NAME) as sender:
        sender.send_messages(ServiceBusMessage(body))
        print(f"Sent malformed test message to queue: {QUEUE_NAME}")
        print(f"Body: {body}")
        print()
        print("The child_story_processor will pick this up, fail to parse it,")
        print("and abandon it. After max_delivery_count retries, Service Bus")
        print("moves it to the dead-letter sub-queue.")
