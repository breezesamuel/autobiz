"""semantic —— 统一语义命令层。

不管底层是 Serial/HID/Modbus/TCP，对外只有一个函数：
  result = kernel.command(device_sn, "intensity", value=0.8)
  result = kernel.command(device_sn, "temperature")

语义 → 底层字节码 的映射由设备 descriptor 自己提供，
内核不需要为任何具体设备写硬编码——这就是"零硬编码"。

设计：
  platform_vocabulary: {
    "intensity":  {"unit": "",  "range": [0.0, 1.0], "type": "write",
                   "bytecode": lambda v: f"INTENSITY {v:.3f}"},
    "relay":      {"unit": "",  "range": [0, 1],     "type": "write",
                   "bytecode": lambda v: f"RELAY {'ON' if v else 'OFF'}"},
    "temperature":{"unit": "°C","range": [-273, 1000], "type": "read",
                   "bytecode": lambda: "READ temperature"},
  }
  设备 descriptor.capabilities ∩ platform_vocabulary → 交集
  交集 = 设备能理解、内核也能翻译的命令
  差集（设备有内核没有）→ 内核注册进来（自进化）
"""
from __future__ import annotations

import json
from typing import Any, Callable


# ---------- 平台词汇表（内核原生能说的话）----------

PLATFORM_VOCABULARY: dict[str, dict[str, Any]] = {
    # 传感器
    "temperature": {"type": "read",   "unit": "°C", "range": [-273.15, 1000],
                    "bytecode": lambda v="": "READ temperature"},
    "humidity":    {"type": "read",   "unit": "%",  "range": [0, 100],
                    "bytecode": lambda v="": "READ humidity"},
    "pressure":    {"type": "read",   "unit": "Pa", "range": [0, 1_000_000],
                    "bytecode": lambda v="": "READ pressure"},
    "light":       {"type": "read",   "unit": "lux","range": [0, 100_000],
                    "bytecode": lambda v="": "READ light"},
    "voltage":     {"type": "read",   "unit": "V",  "range": [-500, 500],
                    "bytecode": lambda v="": "READ voltage"},
    "current":     {"type": "read",   "unit": "A",  "range": [-100, 100],
                    "bytecode": lambda v="": "READ current"},
    # 执行器
    "led":         {"type": "write",  "unit": "",  "range": [0, 1],
                    "bytecode": lambda v: f"LED {int(v)}"},
    "intensity":   {"type": "write",  "unit": "",  "range": [0.0, 1.0],
                    "bytecode": lambda v: f"INTENSITY {float(v):.4f}"},
    "relay":       {"type": "write",  "unit": "",  "range": [0, 1],
                    "bytecode": lambda v: f"RELAY {'ON' if int(v) else 'OFF'}"},
    "motor":       {"type": "write",  "unit": "",  "range": [-1.0, 1.0],
                    "bytecode": lambda v: f"MOTOR {float(v):.4f}"},
    "servo":       {"type": "write",  "unit": "°", "range": [0, 180],
                    "bytecode": lambda v: f"SERVO {int(v)}"},
    # 执行型命令
    "ping":        {"type": "exec", "bytecode": lambda: "PING"},
    "reset":       {"type": "exec", "bytecode": lambda: "RESET"},
    "describe":    {"type": "exec", "bytecode": lambda: "DESCRIBE"},
}


def translate(name: str, value: Any = None,
              vocab_override: dict[str, Any] | None = None) -> str:
    """语义名 → 可下发的字节码（文本行）。"""
    vocab = vocab_override or PLATFORM_VOCABULARY
    entry = vocab.get(name)
    if not entry:
        # 陌生语义——直接当命令名用（自进化注册后就合法）
        if value is None:
            return name.upper()
        return f"{name.upper()} {value}"
    bc = entry.get("bytecode")
    if not bc:
        return name.upper() if value is None else f"{name.upper()} {value}"
    if value is None:
        return bc() if entry.get("type") in ("read", "exec") else ""
    try:
        return bc(value)
    except Exception:
        return f"{name.upper()} {value}"


def negotiate(descriptor: dict[str, Any],
              vocabulary: dict[str, Any] | None = None) -> dict[str, Any]:
    """能力协商：descriptor.capabilities ∩ vocabulary。

    返回两个集合：
      intersection: {name: cap_entry} —— 内核和设备互相能说
      unknown:      [{name, ...}]     —— 设备有但内核不懂（可自进化）
    """
    vocab = vocabulary or PLATFORM_VOCABULARY
    caps = descriptor.get("capabilities", [])
    intersection: dict[str, Any] = {}
    unknown: list[dict[str, Any]] = []
    for c in caps:
        name = c["name"]
        if name in vocab:
            intersection[name] = c
        else:
            unknown.append(c)
    return {"intersection": intersection, "unknown": unknown}


def add_vocab(name: str, cap: dict[str, Any]) -> None:
    """自进化：把陌生语义注册到平台词汇表。

    安全边界：range 必须存在且合法，type 必须是已知。
    sandbox.py 会调用 validate_capability() 做越界检查。
    """
    from .sandbox import validate_capability
    # validate_capability 要求 cap 必须有 "name" 字段——补齐，避免调用方重复写
    if "name" not in cap:
        cap = dict(cap)  # 不污染传入的 dict
        cap["name"] = name
    validate_capability(cap)
    PLATFORM_VOCABULARY[name] = {
        "type": cap.get("type", "read"),
        "unit": cap.get("unit", ""),
        "range": cap.get("range", []),
        "bytecode": (lambda v="": f"{name.upper()} {v}".strip())
                    if cap.get("type") == "read"
                    else (lambda v: f"{name.upper()} {v}"),
    }
