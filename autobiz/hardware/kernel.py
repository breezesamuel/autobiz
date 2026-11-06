"""kernel —— 自主硬件管理核心。

用法（一行调用 12 步管线）：

    from autobiz.hardware import Kernel, discovery
    k = Kernel()
    eps = discovery.discover_all()
    for ep in eps:
        dev = k.connect(ep)
        if dev:
            result = k.command(dev.sn, "temperature")   # 统一语义命令
            k.command(dev.sn, "intensity", value=0.8)  # 写操作

完整管线（每个 connect() 都会走一遍）：
  [自感知]  OS 枚举 (由 discovery.discover_all 提供)
  [自发现]  transport.open()
  [自识别]  handshake.run() → 读 descriptor → 合法性校验
  [自配置]  registry.assign_pins() → 贪心分配引脚 → 零硬编码
  [自连接]  SDP-1 PING→DESC→CONFIG→ECHO 全部通过
  [自通讯]  semantic.negotiate() → 能力∩词汇表
  [自反馈]  transport.read_line() 闭环回读
  [自记忆]  registry.upsert() → 按 SN 持久化
  [自适应]  同 SN 重插 → 命中上次配置 → 恢复引脚
  [自学习]  learning.record_success/failure → 退避/恢复
  [自进化]  陌生语义 → sandbox.validate_capability → add_vocab 注册
  [自检责]  sandbox.validate_command() → 越界/不可信/只读拒绝
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .descriptor import validate, capability_names, make_mock_descriptor
from .handshake import Handshake, HandshakeError
from .registry import Registry, DeviceEntry, get_registry
from .semantic import negotiate, translate, add_vocab, PLATFORM_VOCABULARY
from .sandbox import (SandboxError, validate_command,
                      validate_capability, check_poll_interval)
from .learning import get_state, LearningState, adaptive_poll_interval


# ---- 设备连接对象（connect() 返回）----

@dataclass
class Device:
    """一台已被内核接管的设备。"""
    sn: str
    descriptor: dict[str, Any]
    entry: DeviceEntry
    transport: Any                          # Transport 实例
    capabilities: dict[str, Any] = field(default_factory=dict)
    unknown_capabilities: list[dict[str, Any]] = field(default_factory=list)
    poll_interval_ms: int = 1000
    connected_at: float = field(default_factory=time.time)
    last_command_ms: float = 0.0

    @property
    def alive(self) -> bool:
        return self.transport.is_open

    @property
    def device_class(self) -> str:
        return self.descriptor.get("device_class", "unknown")

    @property
    def address(self) -> str:
        return self.transport.address


# ---- 核心内核 ----

_global_kernel: "Kernel | None" = None


def get_kernel() -> "Kernel":
    """模块级单例 — 所有 dashboard/handler 共享同一个 Kernel 实例."""
    global _global_kernel
    if _global_kernel is None:
        _global_kernel = Kernel()
    return _global_kernel


class Kernel:
    """单例内核——所有硬件操作的唯一入口。"""

    def __init__(self, registry: Registry | None = None):
        self.registry = registry or get_registry()
        self._connected: dict[str, Device] = {}
        self._learning_states: dict[str, LearningState] = {}
        self._stats = {"connects": 0, "commands": 0, "failures": 0}

    # ========== 12 步管线入口 ==========

    def connect(self, endpoint, transport_factory=None) -> Device | None:
        """接管一台硬件。

        Args:
            endpoint:   discovery.TransportEndpoint（或任意 transport 实例）
            transport_factory:  endpoint 是 dict/endpoint 时，用它建 transport；
                               endpoint 已经是 Transport 实例时直接用
        """
        # ---- 自发现 + 自连接 ----
        if hasattr(endpoint, "write") and hasattr(endpoint, "read_line"):
            t = endpoint  # 已经是 Transport 实例
        else:
            t = self._build_transport(endpoint, transport_factory)
        if t is None:
            return None

        try:
            t.open()
        except Exception as e:
            print(f"[kernel] open {endpoint.address if hasattr(endpoint,'address') else endpoint} failed: {e}")
            return None

        # ---- 自识别 + 自配置 + 自连接（完整握手）----
        hs = Handshake(t)
        try:
            desc = hs.run()
        except HandshakeError as e:
            print(f"[kernel] handshake failed on {t.address}: {e}")
            t.close()
            return None

        sn = desc["serial_number"]

        # ---- 自记忆 + 自适应 ----
        # 同 SN 之前见过吗？命中 → 恢复配置
        existing = self.registry.get(sn)
        if existing and t.address != existing.last_transport:
            print(f"[kernel] adaptive: SN {sn} moved {existing.last_transport} → {t.address}, restoring pins")
        entry = self.registry.upsert(desc, t.address)

        # ---- 自配置（引脚分配，贪心，零硬编码）----
        pins = self.registry.assign_pins(desc, t.address, sn=sn)
        entry.pin_assignments = pins

        # ---- 自通讯（能力协商）----
        neg = negotiate(desc, PLATFORM_VOCABULARY)

        # ---- 自进化：陌生语义自动注册（带边界）----
        for uc in neg["unknown"]:
            try:
                add_vocab(uc["name"], uc)
                print(f"[kernel] auto-evolve: registered '{uc['name']}' for {sn}")
            except SandboxError as se:
                print(f"[kernel] auto-evolve blocked: {se}")

        # ---- 自检责：权限标记（descriptor 里可声明 "readonly": true）----
        perm = "read-only" if desc.get("readonly") else "normal"

        # ---- 自学习状态初始化 ----
        st = get_state(sn)
        self._learning_states[sn] = st

        self._stats["connects"] += 1

        device = Device(
            sn=sn, descriptor=desc, entry=entry, transport=t,
            capabilities=neg["intersection"],
            unknown_capabilities=neg["unknown"],
            poll_interval_ms=check_poll_interval(desc, 1000),
        )
        self._connected[sn] = device
        print(f"[kernel] CONNECTED {sn} via {t.address} "
              f"class={device.device_class} "
              f"caps={len(neg['intersection'])} "
              f"unknown={len(neg['unknown'])}")
        return device

    def disconnect(self, sn: str) -> None:
        dev = self._connected.pop(sn, None)
        if dev:
            try: dev.transport.close()
            except Exception: pass
            self.registry.record_success(sn)  # 干净断开记一次成功

    def disconnect_all(self) -> None:
        for sn in list(self._connected.keys()):
            self.disconnect(sn)

    # ========== 统一语义命令入口 ==========

    def command(self, sn: str, name: str,
                value: Any = None,
                perm: str | None = None) -> dict[str, Any]:
        """对设备发一个语义命令（intensity / temperature / relay 等）。

        完整检查链：
          sandbox.validate_command()    自检责
          learning.can_send()           退避期？
          semantic.translate()          语义 → 字节码
          transport.write() + read_line()  下发 + 回读
          learning.record_*             自学习
        """
        dev = self._connected.get(sn)
        if not dev:
            return {"ok": False, "error": f"device {sn} not connected"}

        desc = dev.descriptor
        perm = perm or ("read-only" if desc.get("readonly") else "normal")

        # 自检责
        try:
            validate_command(desc, name, value, perm)
        except SandboxError as e:
            self._stats["failures"] += 1
            self.registry.record_failure(sn)
            return {"ok": False, "error": str(e)}

        # 自学习退避期？
        st = self._learning_states.get(sn)
        if st and not st.can_send():
            self._stats["failures"] += 1
            wait_s = st.wait_for()
            return {"ok": False, "error": f"in backoff ({wait_s:.1f}s left)"}

        # 语义 → 字节码
        bytecode = translate(name, value)
        if not bytecode:
            return {"ok": False, "error": "empty bytecode"}

        # 下发 + 回读（闭环反馈）
        t0 = time.time()
        try:
            dev.transport.write(bytecode)
            resp = dev.transport.read_line(timeout=5.0)
            latency_ms = (time.time() - t0) * 1000
        except Exception as e:
            latency_ms = (time.time() - t0) * 1000
            self._stats["failures"] += 1
            if st: st.record_failure()
            self.registry.record_failure(sn)
            return {"ok": False, "error": f"transport error: {e}",
                    "latency_ms": round(latency_ms, 1)}

        # 解析回读
        ok = resp is not None and not (resp.startswith("ERR") or resp.startswith("BAD"))
        self._stats["commands"] += 1

        if ok:
            if st: st.record_success()
            self.registry.record_success(sn, latency_ms=latency_ms)
            dev.last_command_ms = latency_ms
        else:
            self._stats["failures"] += 1
            if st: st.record_failure()
            self.registry.record_failure(sn)

        return {
            "ok": ok, "command": name, "value": value,
            "bytecode": bytecode, "response": resp,
            "latency_ms": round(latency_ms, 1),
        }

    # ========== 查询 ==========

    def connected_devices(self) -> list[Device]:
        return list(self._connected.values())

    def get_device(self, sn: str) -> Device | None:
        return self._connected.get(sn)

    def stats(self) -> dict[str, Any]:
        out = dict(self._stats)
        out["connected"] = len(self._connected)
        out["registry_total"] = self.registry.count()
        out["vocab_size"] = len(PLATFORM_VOCABULARY)
        for sn, dev in self._connected.items():
            st = self._learning_states.get(sn)
            out.setdefault("devices", {})[sn] = {
                "class": dev.device_class, "address": dev.address,
                "caps": len(dev.capabilities),
                "learning_factor": st.learning_factor() if st else 0.0,
            }
        return out

    def history(self) -> list[DeviceEntry]:
        return self.registry.all()

    # ========== 内部：build transport ==========

    def _build_transport(self, endpoint, factory=None):
        """按 discovery.TransportEndpoint 动态选 transport。"""
        if factory:
            try:
                return factory(endpoint)
            except Exception:
                return None
        try:
            kind = endpoint.kind if hasattr(endpoint, "kind") else endpoint.get("kind", "")
            address = endpoint.address if hasattr(endpoint, "address") else endpoint.get("address", "")
        except Exception:
            return None

        if kind == "serial":
            from .transports import SerialTransport
            return SerialTransport(address)
        if kind in ("tcp", "modbus", "opcua", "mqtt"):
            from .transports import TCPTransport
            if ":" in address:
                host, port = address.rsplit(":", 1)
                return TCPTransport(host, int(port))
        if kind in ("usb", "hid"):
            # USB/HID 这里留接口——真实 USB 需要 hidapi 或 Cypress driver
            # 当前用 MockTransport 演示全管线
            from .transports import MockTransport
            return MockTransport(address=f"mock://{address}")

        return None
