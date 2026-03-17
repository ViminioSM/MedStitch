import json
import time

from PySide6.QtCore import QDir, QLockFile, Qt
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .controller import enqueue_job, initialize_gui
from .stylesheet import load_styling

_SINGLE_INSTANCE_SERVER = "MedStitch.SingleInstance"
_SERVER_CONNECT_TIMEOUT_MS = 150
_ACK_TIMEOUT_MS = 500
_RETRY_COUNT = 30
_RETRY_DELAY_S = 0.1
_STALE_LOCK_MS = 5000

_server_ref: QLocalServer | None = None
_instance_lock: QLockFile | None = None


def _send_to_existing_instance(payload: dict) -> bool:
    """Try to forward a job payload to an already-running MedStitch instance."""
    sock = QLocalSocket()
    sock.connectToServer(_SINGLE_INSTANCE_SERVER)
    if not sock.waitForConnected(_SERVER_CONNECT_TIMEOUT_MS):
        return False
    sock.write(json.dumps(payload).encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(250)
    if not sock.waitForReadyRead(_ACK_TIMEOUT_MS):
        sock.disconnectFromServer()
        return False
    resp = bytes(sock.readAll().data()).decode("utf-8", errors="replace").strip()
    sock.disconnectFromServer()
    return resp == "OK"


def _start_single_instance_server() -> QLocalServer:
    server = QLocalServer()
    if not server.listen(_SINGLE_INSTANCE_SERVER):
        # Stale server name from crashed process.
        QLocalServer.removeServer(_SINGLE_INSTANCE_SERVER)
        server.listen(_SINGLE_INSTANCE_SERVER)

    def _on_new_connection():
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def _read_and_enqueue():
            try:
                raw = bytes(conn.readAll().data()).decode("utf-8", errors="replace")
                payload = json.loads(raw) if raw else {}
                enqueue_job(
                    preset=payload.get("preset"),
                    input_path=payload.get("input_path"),
                    waifu=bool(payload.get("waifu", False)),
                    watermark=payload.get("watermark"),
                    autostart=bool(payload.get("autostart", True)),
                )
                conn.write(b"OK")
                conn.flush()
            finally:
                conn.disconnectFromServer()

        conn.readyRead.connect(_read_and_enqueue)

    server.newConnection.connect(_on_new_connection)
    return server


def launch(
    *,
    preset: str | None = None,
    input_path: str | None = None,
    waifu: bool = False,
    watermark: bool | None = None,
    autostart: bool = False,
):
    payload = {
        "preset": preset,
        "input_path": input_path,
        "waifu": waifu,
        "watermark": watermark,
        "autostart": autostart,
    }

    # Prevent the startup race where multiple near-simultaneous invocations
    # don't see a running server yet and all open their own windows.
    global _instance_lock
    lock_path = QDir.tempPath() + "/MedStitch.lock"
    _instance_lock = QLockFile(lock_path)
    _instance_lock.setStaleLockTime(5000)

    is_primary = _instance_lock.tryLock(0)
    if not is_primary:
        for _ in range(_RETRY_COUNT):
            if _send_to_existing_instance(payload):
                return
            time.sleep(_RETRY_DELAY_S)

        # Lock may be stale from a crashed process.
        try:
            _instance_lock.removeStaleLockFile()
        except Exception:
            pass
        _instance_lock.tryLock(0)

    # If there is already an instance running, forward the job and exit.
    if (preset or input_path or waifu or (watermark is not None) or autostart) and _send_to_existing_instance(payload):
        return

    app = QApplication([])
    app.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    global _server_ref
    _server_ref = _start_single_instance_server()
    initialize_gui(
        preset=preset,
        input_path=input_path,
        waifu=waifu,
        watermark=watermark,
        autostart=autostart,
    )
    app.setStyleSheet(load_styling())
    app.exec()
