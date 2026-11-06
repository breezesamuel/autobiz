"""TCPTransport —— 裸 TCP socket 对接网络设备。

工业网关 / 树莓派 / ESP32 开 TCP server 时用这个。
"""
from __future__ import annotations

import socket

from .base import Transport


class TCPTransport(Transport):
    def __init__(self, host: str, port: int, timeout: float = 3.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""  # 半包粘包缓冲

    def open(self) -> None:
        if self._sock is not None: return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self._timeout)
        s.connect((self._host, self._port))
        self._sock = s

    def close(self) -> None:
        if self._sock:
            try: self._sock.shutdown(socket.SHUT_RDWR)
            except Exception: pass
            try: self._sock.close()
            except Exception: pass
        self._sock = None

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    @property
    def address(self) -> str:
        return f"{self._host}:{self._port}"

    def write(self, line: str) -> None:
        if not self._sock:
            raise IOError("tcp not open")
        data = (line + "\n").encode("utf-8")
        self._sock.sendall(data)

    def read_line(self, timeout: float = 3.0) -> str | None:
        if not self._sock:
            return None
        old_to = self._sock.gettimeout()
        self._sock.settimeout(timeout)
        try:
            while b"\n" not in self._buf:
                chunk = self._sock.recv(4096)
                if not chunk:
                    return None
                self._buf += chunk
            line, self._buf = self._buf.split(b"\n", 1)
        except socket.timeout:
            return None
        finally:
            self._sock.settimeout(old_to)
        return line.decode("utf-8", errors="replace").rstrip("\r")
