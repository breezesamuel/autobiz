"""MockTransport —— 虚拟/仿真设备 transport。

给内核一个"假装"是真硬件的 transport，
这样就能在没有物理设备的情况下端到端跑通全管线。

内部用一个 dict 当设备状态机：
  - PING → OK
  - DESCRIBE → 返回预注入的 descriptor JSON
  - CONFIG → ACK（空）
  - ECHO command=X value=Y → ECHO_OK
  - 其他任何文本行 → 当成命令，按 descriptor.capabilities 模拟读写值
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
    """一个线程安全的 Mock 设备。"""

    def __init__(self, descriptor: dict[str, Any] | None = None,
                 address: str = "mock://device01"):
        self._address = address
        self._desc = validate(descriptor or make_mock_descriptor())
        self._lock = threading.Lock()
        self._buf: list[str] = []      # 设备排队等内核来读的响应
        self._device_state: dict[str, Any] = {}  # 命令执行结果缓存
        self._open = True
        self._cmd_count = 0
        self._fail_every_n: int | None = None  # 调试：每 N 次命令模拟一次超时

    # ---------- Transport 接口 ----------

    def open(self) -> None: self._open = True

    def close(self) -> None: self._open = False

    @property
    def is_open(self) -> bool: return self._open

    @property
    def address(self) -> str: return self._address

    def write(self, line: str) -> None:
        """内核发命令进来，立即（同一秒）排队设备的响应。"""
        if not self._open:
            raise IOError("mock transport closed")
        with self._lock:
            self._cmd_count += 1
            resp = self._handle(line)
            if resp is not None:
                self._buf.append(resp)

    def read_line(self, timeout: float = 3.0) -> str | None:
        """取排队好的响应。Mock 设备始终瞬时响应。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if self._buf:
                    return self._buf.pop(0)
            time.sleep(0.001)
        return None

    # ---------- Mock 状态机 ----------

    def _handle(self, line: str) -> str | None:
        """把一条内核命令翻译成设备响应。"""
        s = line.strip()

        if s.upper().startswith("PING"):
            return "OK"

        if s.upper().startswith("DESCRIBE"):
            return json.dumps(self._desc, ensure_ascii=False, separators=(",", ":"))

        if s.upper().startswith("CONFIG"):
            return "ACK"

        m = re.match(r"^ECHO\s+command=(\w+)\s+value=(.+)$", s, re.I)
        if m:
            cmd, val = m.group(1), m.group(2)
            return "ECHO_OK"

        # 语义命令——先识别 READ/WRITE/EXEC 前缀（semantic.translate 输出格式）
        m2 = re.match(r"^(READ|WRITE|EXEC)\s+(\w+)\s*(.*)$", s, re.I)
        if m2:
            op, name, rest = m2.group(1).upper(), m2.group(2).lower(), m2.group(3).strip()
            cap = next((c for c in self._desc["capabilities"]
                        if c["name"].lower() == name), None)
            if not cap:
                return f"ERR unknown capability '{name}'"
            ctype = cap.get("type", "read")
            if op == "READ" and ctype in ("read", "rw"):
                return self._mock_read(cap)
            if op == "WRITE" and ctype in ("write", "rw"):
                val = self._parse_value(rest)
                self._device_state[name] = val
                return "OK"
            if op == "EXEC":
                self._device_state[name] = True
                return "OK"
            return f"ERR operation {op} not allowed on '{name}'"

        # 其次：直接命令（无前缀）——比如 "INTENSITY 0.5"
        m3 = re.match(r"^(\w+)\s*(.*)$", s)
        if m3:
            name, rest = m3.group(1).lower(), m3.group(2).strip()
            cap = next((c for c in self._desc["capabilities"]
                        if c["name"].lower() == name), None)
            if not cap:
                return f"ERR unknown capability '{name}'"
            ctype = cap.get("type", "read")
            if ctype == "read":
                return self._mock_read(cap)
            if ctype == "write":
                val = self._parse_value(rest)
                self._device_state[name] = val
                return "OK"
            if ctype == "exec":
                self._device_state[name] = True
                return "OK"

        return None

    def _mock_read(self, cap: dict[str, Any]) -> str:
        """按 range 模拟一个合理的读数。"""
        name = cap["name"]
        rng = cap.get("range") or [-40, 85]
        lo, hi = rng[0], rng[1]
        state_val = self._device_state.get(name)
        if state_val is not None:
            return str(state_val)
        # 给个范围中值
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
