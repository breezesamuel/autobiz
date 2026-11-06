"""Transport 基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Transport(ABC):
    """所有硬件传输层的统一接口。"""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @property
    @abstractmethod
    def address(self) -> str:
        """友好的地址标识（COM3 / 192.168.1.100:502 等）。"""

    @abstractmethod
    def write(self, line: str) -> None:
        """写一行（不含 \\n）。"""

    @abstractmethod
    def read_line(self, timeout: float = 3.0) -> str | None:
        """读一行（不含 \\n），超时返回 None。"""

    def send_bytes(self, raw: bytes) -> bytes:
        """原始字节发送（二进制协议用）。默认把 bytes 转 hex 文本发。"""
        self.write(raw.hex())
        resp = self.read_line()
        if resp:
            try:
                return bytes.fromhex(resp)
            except ValueError:
                return resp.encode()
        return b""
