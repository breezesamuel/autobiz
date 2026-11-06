"""MockTransport —— 虚拟/仿真设备 transport.

给内核一个"假装"是真硬件的 transport，
这样就能在没有物理设备的情况下端到端跑通全管线。

内部用一个 dict 当设备状态机 + 可选 DataBus 共享状态:
  - PING → OK
  - DESCRIBE → 返回预注入的 descriptor JSON
  - CONFIG → ACK（空）
  - ECHO command=X value=Y → ECHO_OK
  - READ 先查 DataBus 再查本地 _device_state
  - WRITE / EXEC 同时写本地 + DataBus (如果 bus 存在)
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from .base import Transport
from ..descriptor import make_mock_descriptor, validate


class MockTransport(Transport):
    """一个线程安全的 Mock 设备.

    可选接 DataBus — 所有设备共享同一个状态空间,
    不再是 120 个各自独立的模拟器.
    """

    def __init__(self, descriptor: dict[str, Any] | None = None,
                 address: str = "mock://device01",
                 bus: Any | None = None):
        self._address = address
        self._desc = validate(descriptor or make_mock_descriptor())
        self._lock = threading.Lock()
        self._buf: list[str] = []
        self._device_state: dict[str, Any] = {}
        self._open = True
        self._cmd_count = 0
        self._fail_every_n: int | None = None
        self._bus = bus

    @property
    def bus(self) -> Any | None:
        return self._bus

    @bus.setter
    def bus(self, b: Any) -> None:
        self._bus = b

    # ---------- Transport 接口 ----------

    def open(self) -> None: self._open = True

    def close(self) -> None: self._open = False

    @property
    def is_open(self) -> bool: return self._open

    @property
    def address(self) -> str: return self._address

    def write(self, line: str) -> None:
        if not self._open:
            raise IOError("mock transport closed")
        with self._lock:
            self._cmd_count += 1
            resp = self._handle(line)
            if resp is not None:
                self._buf.append(resp)

    def read_line(self, timeout: float = 3.0) -> str | None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if self._buf:
                    return self._buf.pop(0)
            time.sleep(0.001)
        return None

    # ---------- Mock 状态机 ----------

    def _handle(self, line: str) -> str | None:
        s = line.strip()

        if s.upper().startswith("PING"):
            return "OK"

        if s.upper().startswith("DESCRIBE"):
            return json.dumps(self._desc, ensure_ascii=False, separators=(",", ":"))

        if s.upper().startswith("CONFIG"):
            return "ACK"

        m = re.match(r"^ECHO\s+command=(\w+)\s+value=(.+)$", s, re.I)
        if m:
            return "ECHO_OK"

        # 语义命令
        m2 = re.match(r"^(READ|WRITE|EXEC)\s+(\w+)\s*(.*)$", s, re.I)
        if m2:
            op, name, rest = m2.group(1).upper(), m2.group(2).lower(), m2.group(3).strip()
            cap = next((c for c in self._desc["capabilities"]
                        if c["name"].lower() == name), None)
            if not cap:
                return f"ERR unknown capability '{name}'"
            ctype = cap.get("type", "read")
            key = f"{self._desc['serial_number']}.{name}"
            if op == "READ" and ctype in ("read", "rw"):
                return self._mock_read(cap, name)
            if op == "WRITE" and ctype in ("write", "rw"):
                val = self._parse_value(rest)
                self._device_state[name] = val
                self._bus_set(key, val)
                return "OK"
            if op == "EXEC":
                self._device_state[name] = True
                self._bus_set(key, True)
                return "OK"
            return f"ERR operation {op} not allowed on '{name}'"

        # 裸命令
        m3 = re.match(r"^(\w+)\s*(.*)$", s)
        if m3:
            name, rest = m3.group(1).lower(), m3.group(2).strip()
            cap = next((c for c in self._desc["capabilities"]
                        if c["name"].lower() == name), None)
            if not cap:
                return f"ERR unknown capability '{name}'"
            ctype = cap.get("type", "read")
            key = f"{self._desc['serial_number']}.{name}"
            if ctype == "read":
                return self._mock_read(cap, name)
            if ctype == "write":
                val = self._parse_value(rest)
                self._device_state[name] = val
                self._bus_set(key, val)
                return "OK"
            if ctype == "exec":
                self._device_state[name] = True
                self._bus_set(key, True)
                return "OK"

        return None

    def _bus_set(self, key: str, val: Any) -> None:
        """如果 bus 存在, 同步写入."""
        if self._bus is not None:
            try:
                self._bus.set(key, val)
            except Exception:
                pass

    def _mock_read(self, cap: dict[str, Any], name_hint: str = "") -> str:
        """优先查 bus, 再本地 _device_state, 最后 range midpoint."""
        name = name_hint or cap["name"]
        # 1) 先查 bus
        if self._bus is not None:
            try:
                val = self._bus.get(f"{self._desc['serial_number']}.{name}")
                if val is not None:
                    return str(val)
            except Exception:
                pass
        # 2) 再查本地 _device_state
        state_val = self._device_state.get(name)
        if state_val is not None:
            return str(state_val)
        # 3) range midpoint
        rng = cap.get("range") or [-40, 85]
        lo, hi = rng[0], rng[1]
        mid = (lo + hi) / 2
        return str(round(mid, 2) if isinstance(mid, float) else mid)

    @staticmethod
    def _parse_value(s: str) -> Any:
        s = s.strip().lower()
        if s in ("true", "on", "1"): return 1
        if s in ("false", "off", "0"): return 0
        try: return int(s)
        except ValueError: pass
        try: return float(s)
        except ValueError: pass
        return s

    # ---------- 测试辅助 ----------

    def set_fail_every(self, n: int) -> None:
        self._fail_every_n = n

    def set_state(self, key: str, val: Any) -> None:
        self._device_state[key] = val
