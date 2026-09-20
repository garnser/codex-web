from __future__ import annotations

import os
import socket


def sd_notify(message: str) -> bool:
    """Send a systemd notification when NOTIFY_SOCKET is configured."""

    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return False
    address: str | bytes = notify_socket
    if notify_socket.startswith("@"):
        address = "\0" + notify_socket[1:]
    try:
        with socket.socket(
            socket.AF_UNIX,
            socket.SOCK_DGRAM,
        ) as client:
            client.connect(address)
            client.sendall(message.encode())
        return True
    except OSError:
        return False


def run_server() -> None:
    """Run the composed FastAPI application through the public server module."""

    import uvicorn

    host = os.environ.get("CODEX_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("CODEX_WEB_PORT", "8765"))
    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=False,
        timeout_graceful_shutdown=10,
    )
