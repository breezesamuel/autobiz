"""SerialTransport —— 真实串口 / USB-UART。

超时保护：
  - 打开端口失败 → SerialError 往上抛
  - write 不阻塞（pyserial 默认 timeout=0 非阻塞）
  - read_line 用 ser.readline(timeout=...) 带超时
"""
from __future__ import annotations

import io
import serial
from serial import SerialException

from .base import Transport


class SerialTransport(Transport):
    def __init__(self, device: str, baudrate: int = 9600,
                 timeout: float = 1.0, **kwargs):
        self._device = device
        self._baudrate = baudrate
        self._timeout = timeout
        self._ser: serial.Serial | None = None
        self._extra = kwargs

    def open(self) -> None:
        if self._ser is not None:
            return
        try:
            self._ser = serial.Serial(
                port=self._device, baudrate=self._baudrate,
                timeout=self._timeout, write_timeout=self._timeout,
                **self._extra,
            )
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
        except SerialException as e:
            self._ser = None
            raise SerialException(f"open {self._device} failed: {e}") from e

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            try: self._ser.close()
            except Exception: pass
        self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    @property
    def address(self) -> str:
        return self._device

    def write(self, line: str) -> None:
        if not self.is_open:
            raise IOError(f"{self._device} not open")
        self._ser.write((line + "\n").encode("utf-8", errors="replace"))

    def read_line(self, timeout: float = 3.0) -> str | None:
        if not self.is_open:
            return None
        # 临时把 timeout 改成需要的值
        old_to = self._ser.timeout
        try:
            self._ser.timeout = timeout
            raw = self._ser.readline()
        finally:
            self._ser.timeout = old_to
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")
