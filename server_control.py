from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from organizer.runtime_revision import application_revision


PROJECT_ROOT = Path(__file__).resolve().parent


def data_root() -> Path:
    return Path(os.environ.get("AI_BOOKS_DATA_DIR", PROJECT_ROOT / "data"))


def runtime_file() -> Path:
    return data_root() / "server.json"


def read_runtime() -> dict[str, Any] | None:
    try:
        payload = json.loads(runtime_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload.get("port"), int) or not payload.get("instanceId"):
        return None
    return payload


def server_url(state: dict[str, Any]) -> str:
    return f"http://127.0.0.1:{state['port']}"


def server_is_running(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    try:
        with urllib.request.urlopen(f"{server_url(state)}/api/health", timeout=1) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return payload.get("status") == "ok" and payload.get("instanceId") == state["instanceId"]


def remove_stale_runtime() -> None:
    try:
        runtime_file().unlink(missing_ok=True)
    except OSError:
        pass


def start_server(open_browser: bool = True) -> int:
    current = read_runtime()
    if server_is_running(current):
        revision = application_revision(PROJECT_ROOT)
        if current.get("revision") == revision:
            url = server_url(current)
            print(f"Server is already running at {url}")
            if open_browser:
                webbrowser.open(url)
            return 0
        print("Application files changed. Restarting the server.")
        result = stop_server()
        if result:
            return result
    remove_stale_runtime()
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / "server.log"
    environment = os.environ.copy()
    environment["AI_BOOKS_NO_BROWSER"] = "1"
    options: dict[str, Any] = {
        "cwd": PROJECT_ROOT,
        "env": environment,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    with log_path.open("a", encoding="utf-8") as log:
        options["stdout"] = log
        options["stderr"] = subprocess.STDOUT
        process = subprocess.Popen([sys.executable, str(PROJECT_ROOT / "run.py")], **options)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"Server failed to start. Read {log_path}", file=sys.stderr)
            return 1
        current = read_runtime()
        if server_is_running(current):
            url = server_url(current)
            print(f"Server started at {url}")
            if open_browser:
                webbrowser.open(url)
            return 0
        time.sleep(0.2)
    print(f"Server did not become ready. Read {log_path}", file=sys.stderr)
    return 1


def stop_server() -> int:
    current = read_runtime()
    if not server_is_running(current):
        remove_stale_runtime()
        print("Server is not running.")
        return 0
    request = urllib.request.Request(
        f"{server_url(current)}/api/runtime/shutdown",
        method="POST",
        headers={"X-Instance-ID": current["instanceId"]},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError(f"Unexpected response: {response.status}")
    except (OSError, urllib.error.URLError) as exc:
        print(f"Could not stop the server: {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not server_is_running(current):
            remove_stale_runtime()
            print("Server stopped.")
            return 0
        time.sleep(0.2)
    print("Server received the stop request but is still running.", file=sys.stderr)
    return 1


def main() -> int:
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "start"
    open_browser = os.environ.get("AI_BOOKS_CONTROL_NO_BROWSER") != "1"
    if command == "start":
        return start_server(open_browser=open_browser)
    if command == "stop":
        return stop_server()
    if command == "restart":
        result = stop_server()
        return result if result else start_server(open_browser=open_browser)
    if command == "status":
        current = read_runtime()
        if server_is_running(current):
            print(f"Server is running at {server_url(current)}")
            return 0
        print("Server is not running.")
        return 1
    print("Usage: server_control.py start|stop|restart|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
