from __future__ import annotations

import json
import os
import socket
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import uvicorn

from organizer.runtime_revision import application_revision


PROJECT_ROOT = Path(__file__).resolve().parent


def runtime_file() -> Path:
    data_root = Path(os.environ.get("AI_BOOKS_DATA_DIR", PROJECT_ROOT / "data"))
    data_root.mkdir(parents=True, exist_ok=True)
    return data_root / "server.json"


def write_runtime(path: Path, *, port: int, instance_id: str, revision: str) -> None:
    payload = {
        "pid": os.getpid(),
        "port": port,
        "instanceId": instance_id,
        "revision": revision,
        "startedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def clear_runtime(path: Path, instance_id: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("instanceId") == instance_id:
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def available_port() -> int:
    for port in range(8765, 8775):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("لم يتوفر منفذ محلي من 8765 إلى 8774")


if __name__ == "__main__":
    port = available_port()
    instance_id = uuid4().hex
    os.environ["AI_BOOKS_INSTANCE_ID"] = instance_id
    state_path = runtime_file()
    write_runtime(
        state_path,
        port=port,
        instance_id=instance_id,
        revision=application_revision(PROJECT_ROOT),
    )
    if os.environ.get("AI_BOOKS_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        uvicorn.run("organizer.main:app", host="127.0.0.1", port=port, reload=False)
    finally:
        clear_runtime(state_path, instance_id)
