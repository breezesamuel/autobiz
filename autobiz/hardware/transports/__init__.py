"""transports —— 可插拔传输层适配器。

所有 transport 实现相同接口：
  write(line: str) -> None      # 发一行（自动加 \\n）
  read_line(timeout: float) -> str | None  # 收一行
  send_bytes(raw: bytes) -> bytes  # 原始字节发送（Modbus/OPC UA 用）
  close() -> None
  is_open -> bool

内核只认这个接口，不关心底下是 COM/TCP/HID/Modbus。
"""
from .base import Transport
from .mock import MockTransport
from .serial import SerialTransport
from .tcp import TCPTransport

__all__ = ["Transport", "MockTransport", "SerialTransport", "TCPTransport"]
