"""handshake —— SDP-1 握手协议（Serial Discovery Protocol v1）。

任何 transport 连上后，按这个顺序对话：

  Kernel  → transport.write("PING\\n")
  Device  → transport.read("OK\\n")
  Kernel  → transport.write("DESCRIBE\\n")
  Device  → transport.read(<valid JSON descriptor>)
  Kernel  → transport.write("CONFIG pinout=<assignments>\\n")
  Device  → transport.read("ACK\\n")
  Kernel  → transport.write("ECHO command='intensity', value=0.8\\n")
  Device  → transport.read("ECHO_OK\\n")

设备可以少走几步（比如 CONFIG 可以是 "SKIP"、ECHO 可以直接跳过），
但 PING → DESCRIBE 是必须的——没有描述符就没有自识别。

所有命令用单行文本 + \\n 分隔，JSON 用一整行（无换行）。
这就够 Serial / TCP / HID / Modbus RTU 都能适配。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from .descriptor import validate, DescriptorError, DescriptorError as DE


HANDSHAKE_TIMEOUT_PER_STEP = 3.0   # 每步超时
HANDSHAKE_MAX_TOTAL = 15.0        # 总超时

PING_RE = re.compile(r"^PING(?:\s+.*)?$")
DESCRIBE_RE = re.compile(r"^DESCRIBE(?:\s+.*)?$")
CONFIG_RE  = re.compile(r"^CONFIG\s+(.+)$")
ECHO_RE    = re.compile(r"^ECHO\s+(.+)$")

RESP_OK = re.compile(r"^OK\b")
RESP_ACK = re.compile(r"^ACK\b")
RESP_ECHO_OK = re.compile(r"^ECHO_OK\b")


class HandshakeError(Exception):
    pass


class Handshake:
    """SDP-1 握手状态机。

    transport 必须实现两个方法：
      write(line: str) -> None
      read_line(timeout: float) -> str | None
    """

    def __init__(self, transport, timeout: float = HANDSHAKE_MAX_TOTAL):
        self.t = transport
        self.timeout = timeout
        self.descriptor: dict[str, Any] | None = None
        self.pin_assignments: dict[int, str] = {}

    # ---------- 内部 ----------

    def _send(self, line: str) -> None:
        self.t.write(line + "\n")

    def _recv(self, step_timeout: float = HANDSHAKE_TIMEOUT_PER_STEP) -> str:
        line = self.t.read_line(timeout=step_timeout)
        if line is None:
            raise HandshakeError("timeout waiting for response")
        return line.strip()

    # ---------- 公开 ----------

    def run(self) -> dict[str, Any]:
        """完整握手，返回 validated descriptor + pin 分配结果。"""
        t0 = time.time()

        # Step 1: PING
        self._send("PING")
        if not RESP_OK.match(self._recv()):
            raise HandshakeError("PING not acknowledged")

        # Step 2: DESCRIBE
        self._send("DESCRIBE")
        raw = self._recv(step_timeout=5.0)  # JSON 可能长一点
        # 设备可能回 JSON 纯文本或者 `DESC <json>`
        if raw.startswith("DESC "):
            raw = raw[5:].strip()
        try:
            desc = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HandshakeError(f"DESCRIBE invalid JSON: {e}")
        try:
            desc = validate(desc)
        except DescriptorError as e:
            raise HandshakeError(f"descriptor invalid: {e}")
        self.descriptor = desc

        # Step 3: CONFIG（可选）——让内核的 registry 决定引脚分配
        pinout = desc.get("pinout") or []
        if pinout:
            self._send("CONFIG pinout=REQUEST")
            resp = self._recv()
            # SKIP / ACK <payload>
            if resp.startswith("SKIP"):
                pass  # 设备自己管引脚
            elif RESP_ACK.match(resp):
                try:
                    payload = resp.split(None, 1)[1] if " " in resp else "{}"
                    self.pin_assignments = json.loads(payload)
                except Exception:
                    self.pin_assignments = {}

        # Step 4: ECHO —— 验证语义命令通道通
        caps = desc.get("capabilities", [])
        if caps:
            sample_name = caps[0]["name"]
            self._send(f"ECHO command={sample_name} value=test")
            resp = self._recv()
            if not RESP_ECHO_OK.match(resp):
                # ECHO 失败不致命——有些设备没实现回显
                pass

        elapsed = time.time() - t0
        desc["_handshake_elapsed"] = round(elapsed, 3)
        return desc


def build_ping_only_response() -> str:
    """Mock/虚拟设备侧用：内核发 PING 时回 OK。"""
    return "OK"


def build_describe_response(desc: dict[str, Any]) -> str:
    """Mock/虚拟设备侧用：内核发 DESCRIBE 时回合法 JSON。"""
    return json.dumps(desc, ensure_ascii=False, separators=(",", ":"))
