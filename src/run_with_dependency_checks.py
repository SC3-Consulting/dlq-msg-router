"""
This module wraps the main entry point of the agent with dependency checks for the Service Bus emulator.
- If the connection string indicates that the emulator is being used, it will wait for the AMQP endpoint and the health endpoint to be ready before starting the agent.
- If the connection string does not indicate the emulator, it will skip the dependency checks and start the agent immediately.
- The dependency checks can be disabled entirely by setting ENABLE_DEPENDENCY_WAIT=false in the environment
"""

import os
import re
import socket
import time
import urllib.request

from src import run_agent


def _extract_host_port_from_connection_string(conn_str):
    """Extracts the host and port from a Service Bus connection string.
    Args:
        conn_str (str): The Service Bus connection string.
    Returns:
        Tuple[str, int]: A tuple containing the host and port. If the port is not specified, defaults to 5672.
    """
    if not conn_str:
        return None, None

    match = re.search(r"Endpoint\s*=\s*sb://([^/;]+)", conn_str, re.IGNORECASE)
    if not match:
        return None, None

    host_port = match.group(1).strip()
    if ":" in host_port:
        host, port_str = host_port.rsplit(":", 1)
        try:
            return host, int(port_str)
        except ValueError:
            return host, 5672

    return host_port, 5672


def _wait_for_tcp(host, port, timeout_seconds):
    """Waits for a TCP connection to the specified host and port to be available within the given timeout.
    Args:
        host (str): The hostname or IP address to connect to.
        port (int): The port number to connect to.
        timeout_seconds (int): The maximum time to wait for the connection, in seconds.
    Returns:
        bool: True if the connection was successful within the timeout, False otherwise.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


def _wait_for_http(url, timeout_seconds):
    """Waits for an HTTP endpoint to be available within the given timeout.
    Args:
        url (str): The URL of the HTTP endpoint to check.
        timeout_seconds (int): The maximum time to wait for the endpoint, in seconds.
    Returns:
        bool: True if the endpoint was available within the timeout, False otherwise.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 300:
                    return True
        except Exception:
            time.sleep(1)
    return False


def _should_wait_for_emulator(connection_string):
    """Determines if the agent should wait for the Service Bus emulator based on the connection string.
    Args:
        connection_string (str): The Service Bus connection string.
    Returns:
        bool: True if the connection string indicates that the emulator is being used, False otherwise.
    """
    if not connection_string:
        return False

    lowered = connection_string.lower()
    if "usedevelopmentemulator=true" in lowered:
        return True

    host, _ = _extract_host_port_from_connection_string(connection_string)
    if not host:
        return False

    return host in {"servicebus-emulator", "localhost", "host.docker.internal"}


def _run_dependency_checks():
    """Runs dependency checks for the Service Bus emulator if the connection string indicates that the emulator is being used.
    - Waits for the AMQP endpoint to be available.
    - Waits for the health endpoint to be available.
    - Skips checks if ENABLE_DEPENDENCY_WAIT is set to false.
    """
    if os.getenv("ENABLE_DEPENDENCY_WAIT", "true").lower() != "true":
        print("[startup] Dependency wait disabled via ENABLE_DEPENDENCY_WAIT.")
        return

    conn_str = os.getenv("SERVICE_BUS_CONNECTION_STRING", "")
    if not _should_wait_for_emulator(conn_str):
        return

    host, amqp_port = _extract_host_port_from_connection_string(conn_str)
    health_port = int(os.getenv("EMULATOR_HTTP_PORT", "5300"))
    wait_timeout = int(os.getenv("DEPENDENCY_WAIT_TIMEOUT_SECONDS", "120"))

    if not host:
        print(
            "[startup] Emulator dependency wait skipped: could not parse connection string endpoint."
        )
        return

    print(
        f"[startup] Waiting for Service Bus emulator dependencies: host={host}, amqp_port={amqp_port}, health_port={health_port}, timeout={wait_timeout}s"
    )

    if not _wait_for_tcp(host, amqp_port, wait_timeout):
        print(
            f"[startup] WARNING: AMQP endpoint {host}:{amqp_port} not ready within timeout; continuing startup."
        )
        return

    health_url = f"http://{host}:{health_port}/health"
    if not _wait_for_http(health_url, wait_timeout):
        print(
            f"[startup] WARNING: Emulator health endpoint {health_url} not ready within timeout; continuing startup."
        )
        return

    print("[startup] Service Bus emulator dependency checks passed.")


def main():
    """Main entry point for running the agent with dependency checks for the Service Bus emulator.
    - If the connection string indicates that the emulator is being used, it will wait for the AMQP endpoint and the health endpoint to be ready before starting the agent.
    - If the connection string does not indicate the emulator, it will skip the dependency checks and start the agent immediately.
    - The dependency checks can be disabled entirely by setting ENABLE_DEPENDENCY_WAIT=false in the environment.
    """
    _run_dependency_checks()
    run_agent.main()


if __name__ == "__main__":
    main()
