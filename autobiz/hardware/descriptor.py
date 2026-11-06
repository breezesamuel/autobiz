"""descriptor —— 设备自我描述协议（Hardware Descriptor Protocol, HDP）。

任何硬件通过任意 transport（Serial/TCP/HID/Modbus）连上后，
只要能说出这份 JSON，内核就能自动识别它、理解它、控制它。

设备回应 DESCRIBE 命令 → 返回一个 JSON dict，必须包含：
  protocol_version: "hdp-1"
  vendor_id / product_id:   厂商/产品 ID（USB VID/PID 或自定义）
  device_class:              "sensor" / "actuator" / "io" / "gateway" / "unknown"
  serial_number:             唯一 SN（用于自记忆 / 自适应）
  capabilities: [            能力词汇表（内核用语义命令匹配）
    {"name": "temperature", "type": "read",   "unit": "°C", "range": [-40, 85]},
    {"name": "humidity",    "type": "read",   "unit": "%",  "range": [0, 100]},
    {"name": "led",         "type": "write", "unit": "",   "range": [0, 1]},
    {"name": "relay",       "type": "write", "unit": "",   "range": [0, 1]},
  ]
  pinout: [                  固件引脚表（可选，但有了就能零硬编码）
    {"pin": 1, "role": "VCC",   "voltage": 3.3},
    {"pin": 2, "role": "GND"},
    {"pin": 3, "role": "SDA",   "bus": "I2C"},
    {"pin": 4, "role": "SCL",   "bus": "I2C"},
    {"pin": 5, "role": "GPIO",  "function": "led"},
  ]
  defaults: {                初始配置（可选）
    "poll_interval_ms": 1000,
    "baudrate": 9600,
  }

合法性检查由 validate() 完成——缺字段会在握手阶段被拒绝。
"""
from __future__ import annotations

import json
import re
from typing import Any


REQUIRED_FIELDS = {"protocol_version", "vendor_id", "device_class",
                   "serial_number", "capabilities"}

VALID_CLASSES = {'actuator', 'io', 'energy', 'exoskeleton', 'smart_home', 'medical', 'sensor', 'gateway', 'industrial', 'robot', 'vehicle'}

VALID_CAP_TYPES = {"read", "write", "rw", "notify", "exec"}

MAX_DESC_SIZE = 8192  # 防止恶意大包


class DescriptorError(Exception):
    """descriptor 不合法。"""


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    """校验 descriptor，合法则返回完整 dict（含 defaults 补齐）。"""
    if not isinstance(raw, dict):
        raise DescriptorError("descriptor must be a JSON object")
    if len(json.dumps(raw)) > MAX_DESC_SIZE:
        raise DescriptorError(f"descriptor too large ({MAX_DESC_SIZE} bytes max)")

    missing = REQUIRED_FIELDS - raw.keys()
    if missing:
        raise DescriptorError(f"missing fields: {sorted(missing)}")

    pv = raw["protocol_version"]
    if not isinstance(pv, str) or not re.match(r"^hdp-\d+$", pv):
        raise DescriptorError(f"bad protocol_version: {pv!r} (expected 'hdp-1' etc)")

    if raw["device_class"] not in VALID_CLASSES:
        raise DescriptorError(f"bad device_class: {raw['device_class']!r}")

    if not raw.get("serial_number") or len(str(raw["serial_number"])) > 64:
        raise DescriptorError("serial_number invalid")

    caps = raw["capabilities"]
    if not isinstance(caps, list) or not caps:
        raise DescriptorError("capabilities must be non-empty list")

    for i, c in enumerate(caps):
        if not isinstance(c, dict):
            raise DescriptorError(f"cap[{i}] not dict")
        if "name" not in c:
            raise DescriptorError(f"cap[{i}] missing name")
        if c.get("type", "read") not in VALID_CAP_TYPES:
            raise DescriptorError(f"cap[{i}] bad type: {c.get('type')}")

    # defaults 补齐
    raw.setdefault("protocol_version", "hdp-1")
    raw.setdefault("product_id", raw.get("device_class", "unknown"))
    raw.setdefault("pinout", [])
    raw.setdefault("defaults", {"poll_interval_ms": 1000})
    raw["defaults"].setdefault("poll_interval_ms", 1000)
    return raw


def make_mock_descriptor(
    vendor_id: str = "mock-vendor",
    device_class: str = "sensor",
    sn: str | None = None,
    caps: list[dict[str, Any]] | None = None,
    name: str = "GenericMock",
) -> dict[str, Any]:
    """快速构造一个合法 descriptor（用于测试 / Mock 设备）。"""
    import uuid
    if sn is None:
        sn = f"SN-{uuid.uuid4().hex[:12].upper()}"
    if caps is None:
        if device_class == "sensor":
            caps = [
                {"name": "temperature", "type": "read", "unit": "°C", "range": [-40, 85]},
                {"name": "humidity",    "type": "read", "unit": "%",  "range": [0, 100]},
            ]
        elif device_class == "actuator":
            caps = [
                {"name": "led",     "type": "write", "unit": "", "range": [0, 1]},
                {"name": "intensity","type": "write", "unit": "", "range": [0.0, 1.0]},
                {"name": "relay",   "type": "write", "unit": "", "range": [0, 1]},
            ]
        else:
            caps = [{"name": "ping", "type": "exec"}]
    return validate({
        "protocol_version": "hdp-1",
        "vendor_id": vendor_id,
        "product_id": name,
        "device_class": device_class,
        "serial_number": sn,
        "capabilities": caps,
        "pinout": [
            {"pin": 1, "role": "VCC", "voltage": 3.3},
            {"pin": 2, "role": "GND"},
            {"pin": 3, "role": "GPIO", "function": caps[0]["name"]},
        ],
        "defaults": {"poll_interval_ms": 500},
    })


def capability_names(desc: dict[str, Any]) -> set[str]:
    """返回设备能理解的语义词汇集。"""
    return {c["name"] for c in desc.get("capabilities", [])}
