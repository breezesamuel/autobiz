"""registry —— 设备注册表。

三件事：
  1. 已发现设备按 SN 持久化到 JSON 文件（自记忆）
  2. 新 transport 命中已知 SN → 恢复上次配置（自适应）
  3. 固件引脚表 → 贪心分配 → 零冲突（自配置）

持久化格式：
  data/hardware_registry.json
    {
      "SN-XXXX": {
        "descriptor": {...},       # 最后一次握手拿到的
        "last_transport": "COM3",  # 上次连接的口子
        "pin_assignments": {3: "SDA", 4: "SCL", 5: "led"},
        "first_seen": "2026-11-05T...",
        "last_seen": "2026-11-05T...",
        "stats": {"failures": 0, "successes": 42, "avg_latency_ms": 32},
      }
    }
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .descriptor import validate


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "data" / "hardware_registry.json"


class DeviceEntry:
    """注册表中的一台设备。"""
    __slots__ = ("sn", "descriptor", "last_transport",
                 "pin_assignments", "first_seen", "last_seen", "stats")

    def __init__(self, sn: str, descriptor: dict[str, Any],
                 last_transport: str = "", pin_assignments: dict[int, str] | None = None):
        self.sn = sn
        self.descriptor = descriptor
        self.last_transport = last_transport
        self.pin_assignments: dict[int, str] = pin_assignments or {}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.first_seen = now
        self.last_seen = now
        self.stats: dict[str, Any] = {
            "failures": 0, "successes": 0, "avg_latency_ms": 0.0,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "sn": self.sn, "descriptor": self.descriptor,
            "last_transport": self.last_transport,
            "pin_assignments": self.pin_assignments,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "stats": self.stats,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeviceEntry":
        e = cls(d["sn"], d.get("descriptor") or {}, d.get("last_transport", ""))
        e.pin_assignments = {int(k): v for k, v in (d.get("pin_assignments") or {}).items()}
        e.first_seen = d.get("first_seen", "")
        e.last_seen = d.get("last_seen", "")
        e.stats = d.get("stats") or {"failures": 0, "successes": 0, "avg_latency_ms": 0.0}
        return e


class Registry:
    """单例注册表。"""

    def __init__(self, path: Path = REGISTRY_PATH):
        self.path = path
        self._entries: dict[str, DeviceEntry] = {}
        self._pin_pool: dict[str, set[int]] = {}  # 每个 transport 已用引脚
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for sn, d in data.items():
                self._entries[sn] = DeviceEntry.from_dict(d)
        except Exception as e:
            print(f"[registry] load failed: {e}")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {sn: e.to_dict() for sn, e in self._entries.items()}
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    # ---------- 查询 ----------

    def get(self, sn: str) -> DeviceEntry | None:
        return self._entries.get(sn)

    def all(self) -> list[DeviceEntry]:
        return list(self._entries.values())

    def count(self) -> int:
        return len(self._entries)

    # ---------- 登记 / 更新 ----------

    def upsert(self, descriptor: dict[str, Any], transport_address: str) -> DeviceEntry:
        """新设备登记或已有设备重新连接（自适应）。"""
        desc = validate(descriptor)
        sn = desc["serial_number"]
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        existing = self._entries.get(sn)
        if existing:
            existing.descriptor = desc
            existing.last_transport = transport_address
            existing.last_seen = now
            # transport 变了 → 尝试恢复旧引脚分配（自适应核心逻辑）
            if transport_address != existing.last_transport and existing.pin_assignments:
                # 先放进去，等 self.assign_pins() 在新 transport 上再确认
                pass
        else:
            existing = DeviceEntry(sn, desc, transport_address)
            self._entries[sn] = existing
        self.save()
        return existing

    def record_success(self, sn: str, latency_ms: float = 0.0) -> None:
        e = self._entries.get(sn)
        if not e: return
        e.stats["successes"] += 1
        if latency_ms > 0:
            # Welford running average
            n = e.stats["successes"]
            old = e.stats["avg_latency_ms"]
            e.stats["avg_latency_ms"] = old + (latency_ms - old) / n
        self.save()

    def record_failure(self, sn: str) -> None:
        e = self._entries.get(sn)
        if not e: return
        e.stats["failures"] += 1
        self.save()

    # ---------- 引脚贪心分配（自配置核心）----------

    def _used_pins(self, transport: str) -> set[int]:
        return self._pin_pool.setdefault(transport, set())

    def reset_pins(self, transport: str) -> None:
        """设备断开时释放该 transport 的引脚池。"""
        self._pin_pool.pop(transport, None)

    def assign_pins(self, descriptor: dict[str, Any],
                    transport: str,
                    sn: str | None = None) -> dict[int, str]:
        """贪心分配——拿设备固件 pinout，避开已用的。

        冲突策略：
          - 电源引脚（VCC/GND）允许共享（同 transport 多设备可共地）
          - 总线引脚（SDA/SCL/TX/RX）按 bus 名称独占
          - GPIO 引脚独占，避开已用
          - 同 SN 重连：优先用上次分配过的（自适应）
        """
        pinout = descriptor.get("pinout") or []
        used = self._used_pins(transport)

        # 同 SN 恢复逻辑
        if sn and sn in self._entries:
            prev = self._entries[sn].pin_assignments
            if prev:
                # 先试恢复：这些引脚如果还没被别人占就直接用
                restored = {}
                for pin, role in prev.items():
                    if pin not in used or role in ("VCC", "GND"):
                        restored[pin] = role
                        if role not in ("VCC", "GND"):
                            used.add(pin)
                if len(restored) >= len([p for p in pinout if p.get("role") not in ("VCC", "GND")]):
                    # 够用了，直接返回
                    return restored

        result: dict[int, str] = {}
        bus_taken: dict[str, int] = {}  # I2C -> SDA pin

        for p in pinout:
            pin = p.get("pin")
            role = p.get("role", "GPIO")
            if pin is None:
                continue
            if role in ("VCC", "GND"):
                result[pin] = role  # 直接分配，不占池
                continue
            bus = p.get("bus")
            if bus:
                key = f"{bus}:{role}"
                if role == "SDA" or role in ("TX", "MOSI", "CS"):
                    if bus in bus_taken:
                        # 同 bus 已被别的设备占用 → 标记冲突
                        used.add(pin)
                        continue
                    bus_taken[bus] = pin
                used.add(pin)
                result[pin] = role
                continue
            # 普通 GPIO：找下一个没被用的脚
            if pin in used:
                # 冲突了——找最近的空位（贪心）
                candidate = pin + 1
                while candidate in used or candidate in result:
                    candidate += 1
                    if candidate > 40:  # 安全上限
                        break
                pin = candidate
            used.add(pin)
            result[pin] = role

        if sn and sn in self._entries:
            self._entries[sn].pin_assignments = result
            self.save()
        return result


# 全局单例
_registry: Registry | None = None


def get_registry(path: Path | None = None) -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry(path=path or REGISTRY_PATH)
    return _registry
