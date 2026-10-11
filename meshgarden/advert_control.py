"""Local operator requests, handled through the daemon's existing radio connection."""

from __future__ import annotations

import json
import logging
import os
import socket
import stat
from collections.abc import Callable

log = logging.getLogger(__name__)


def socket_path(database_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(database_path)), "advert.sock")


class AdvertControl:
    """Owner-only Unix socket polled by the daemon's DB worker, one request per tick."""

    def __init__(self, path: str, advertise: Callable[[], tuple[bool, str]]) -> None:
        self.path = path
        self.advertise = advertise
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if os.path.lexists(path):
                if not stat.S_ISSOCK(os.lstat(path).st_mode):
                    raise OSError(f"refusing to replace non-socket path: {path}")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    try:
                        probe.connect(path)
                    except ConnectionRefusedError:
                        os.unlink(path)  # Socket left behind by a crashed daemon.
                    else:
                        raise OSError("advert control socket is already in use")
            self.socket.bind(path)
            os.chmod(path, 0o600)
            self.inode = os.lstat(path).st_ino
            self.socket.listen(4)
            self.socket.setblocking(False)
        except Exception:
            self.socket.close()
            raise

    def poll(self) -> None:
        try:
            conn, _ = self.socket.accept()
        except BlockingIOError:
            return
        with conn:
            conn.settimeout(1)
            try:
                with conn.makefile("rb") as reader:
                    request = reader.readline(65)
                if not request:  # Liveness probes never transmit.
                    return
                if request != b"advert\n":
                    ok, result_detail = False, "unknown control request"
                else:
                    ok, result_detail = self.advertise()
                    # Fixed operator result text, never visitor message content.
                    log.info("operator flood advertisement: %s", result_detail)
                conn.sendall(json.dumps({"ok": ok, "message": result_detail}).encode() + b"\n")
            except OSError:
                log.warning("advert control client disconnected or timed out")
            except Exception:
                log.exception("operator advertisement failed")

    def close(self) -> None:
        self.socket.close()
        try:
            if os.lstat(self.path).st_ino == self.inode:
                os.unlink(self.path)
        except FileNotFoundError:
            pass


def request_advert(database_path: str) -> tuple[bool, str]:
    """Never opens the serial device or retries an uncertain transmission."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15)
        try:
            client.connect(socket_path(database_path))
        except PermissionError:
            return False, "permission denied; run sudo mgarden advert or use the service user"
        except (FileNotFoundError, ConnectionRefusedError):
            return False, "advert control unavailable; start or restart the updated MeshCore daemon"
        client.sendall(b"advert\n")
        try:
            with client.makefile("rb") as reader:
                data = json.loads(reader.readline(2048))
            if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
                raise ValueError("invalid response")
            return data["ok"], str(data["message"])
        except (OSError, ValueError, KeyError):
            return False, "advertisement result unknown; check the daemon log before retrying"
