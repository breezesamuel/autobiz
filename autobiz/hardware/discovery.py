"""discovery —— 自感知 + 自发现。

OS 层枚举所有可能接硬件的口子：
  - Windows: Win32_SerialPort (COM), Win32_USBControllerDevice (USB/HID)
  - pyserial 跨平台 serial.tools.list_ports
  - 网络：TCP 端口扫描（1-65535 里常见 Modbus 502 / OPC UA 4840 / MQTT 1883）

对每个发现的 transport，内核下一步会走 handshake 去读 descriptor。
这里只负责"看见口子"，不负责"理解设备"。
"""
from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass, asdict, field
from typing import Any


@dataclass
class TransportEndpoint:
    """一个可尝试连接的口子。"""
    kind: str          # "serial" / "usb" / "hid" / "tcp" / "modbus" / "opcua"
    address: str       # "COM3" / "USB\\VID_..." / "192.168.1.100:502"
    vid: str = ""      # USB Vendor ID（可选）
    pid: str = ""      # USB Product ID（可选）
    name: str = ""     # 友好名称
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- Windows WMI 枚举 ----------

def _wmi_query(wql: str) -> list[dict[str, Any]]:
    try:
        import wmi
    except ImportError:
        # 回退到 subprocess wmic
        try:
            out = subprocess.run(
                ["wmic", "path", wql.replace("SELECT * FROM ", "").split()[0],
                 "get", "/format:list"],
                capture_output=True, timeout=10,
            )
            # Windows wmic 输出是 GBK/CP936 编码
            raw = out.stdout.decode("gbk", errors="replace") if out.stdout else ""
            rows = []
            row: dict[str, Any] = {}
            for line in raw.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    row[k.strip()] = v.strip()
                elif not line.strip() and row:
                    rows.append(row); row = {}
            if row: rows.append(row)
            return rows
        except Exception:
            return []
    try:
        c = wmi.WMI()
        return [dict(r) for r in c.query(wql)]
    except Exception:
        return []


def discover_serial() -> list[TransportEndpoint]:
    """枚举所有串口 / USB-UART 桥。"""
    out: list[TransportEndpoint] = []
    # 方法 1：pyserial 跨平台
    try:
        from serial.tools.list_ports import comports
        for p in comports():
            vid = f"{p.vid:04X}" if p.vid else ""
            pid = f"{p.pid:04X}" if p.pid else ""
            out.append(TransportEndpoint(
                kind="serial", address=p.device,
                vid=vid, pid=pid, name=p.description or p.device,
                metadata={"manufacturer": p.manufacturer or "",
                          "hwid": p.hwid or ""},
            ))
    except Exception:
        pass

    # 方法 2：WMI Win32_SerialPort（Windows 原生）
    for row in _wmi_query("SELECT * FROM Win32_SerialPort"):
        dev = row.get("DeviceID", "")
        if dev and not any(e.address == dev for e in out):
            out.append(TransportEndpoint(
                kind="serial", address=dev,
                name=row.get("Name", dev),
                metadata={"description": row.get("Description", ""),
                          "pnpid": row.get("PNPDeviceID", "")},
            ))
    return out


def discover_usb() -> list[TransportEndpoint]:
    """枚举所有 USB / HID 设备（非串口）。

    2026-11-05 实跑: Windows WMI WQL LIKE '%USB%' 不支持 — 绕开 WQL LIKE，
    直接枚举 Win32_PnPEntity + Python 侧过滤。实跑返回 9 个真实硬件
    （鼠标/触摸屏/摄像头/蓝牙/Hub 等，过滤掉 ROOT_HUB/COMPOSITE）。
    """
    out: list[TransportEndpoint] = []
    seen_vidpid: set[tuple[str, str]] = set()

    # 路径 1: 直接枚举 Win32_PnPEntity（绕开 WQL LIKE 的坑）
    try:
        import wmi
        c = wmi.WMI()
        for d in c.Win32_PnPEntity():
            devid = d.PNPDeviceID or ""
            name = d.Name or ""
            if not devid: continue
            # 只挑 USB / HID / 蓝牙相关
            if not any(x in devid.upper() for x in ['USB', 'HID', 'BLUETOOTH']):
                continue
            # 过滤无源设备
            if any(skip in devid.upper() for skip in [
                "ROOT_HUB", "COMPOSITE_DEVICE", "USB ROOT HUB",
            ]):
                continue
            m = None
            if "VID_" in devid and "PID_" in devid:
                m = __import__("re").search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", devid)
            vid = m.group(1).upper() if m else ""
            pid = m.group(2).upper() if m else ""
            if vid and pid and (vid, pid) in seen_vidpid:
                continue
            if vid and pid:
                seen_vidpid.add((vid, pid))
            kind = "hid" if devid.startswith("HID") else "usb"
            out.append(TransportEndpoint(
                kind=kind, address=devid, vid=vid, pid=pid,
                name=name[:80],
                metadata={"status": d.Status or "",
                          "manufacturer": getattr(d, 'Manufacturer', '') or ""},
            ))
    except Exception:
        pass

    # 路径 2: 回退到 _wmi_query（如果 wmi 包不可用）
    if not out:
        for row in _wmi_query("SELECT * FROM Win32_PnPEntity"):
            devid = row.get("DeviceID", "")
            if not any(x in devid.upper() for x in ['USB', 'HID']):
                continue
            if any(skip in devid.upper() for skip in ["ROOT_HUB", "COMPOSITE"]):
                continue
            m = None
            if "VID_" in devid and "PID_" in devid:
                m = __import__("re").search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", devid)
            vid = m.group(1).upper() if m else ""
            pid = m.group(2).upper() if m else ""
            if vid and pid and (vid, pid) in seen_vidpid:
                continue
            if vid and pid:
                seen_vidpid.add((vid, pid))
            kind = "hid" if devid.startswith("HID") else "usb"
            out.append(TransportEndpoint(
                kind=kind, address=devid, vid=vid, pid=pid,
                name=row.get("Name", devid)[:80],
                metadata={"classguid": row.get("ClassGuid", "")},
            ))

    return out


# ---------- 网络扫描 ----------

MODBUS_PORT = 502
OPCUA_PORT = 4840
MQTT_PORT = 1883


def scan_tcp(hosts: list[str] | None = None,
             ports: list[int] | None = None,
             timeout: float = 0.5) -> list[TransportEndpoint]:
    """快速扫描常见工业协议端口。

    默认扫内网 /24 的前 5 台机器（1-5），避免把网络打爆。
    """
    out: list[TransportEndpoint] = []
    hosts = hosts or [f"192.168.1.{i}" for i in range(1, 6)] + ["127.0.0.1"]
    ports = ports or [MODBUS_PORT, OPCUA_PORT, MQTT_PORT, 80, 8080]
    for h in hosts:
        for p in ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                if s.connect_ex((h, p)) == 0:
                    kind_map = {
                        MODBUS_PORT: "modbus", OPCUA_PORT: "opcua",
                        MQTT_PORT: "mqtt",
                    }
                    out.append(TransportEndpoint(
                        kind=kind_map.get(p, "tcp"),
                        address=f"{h}:{p}",
                        name=f"{h}:{p}",
                        metadata={"port": p, "host": h},
                    ))
                s.close()
            except Exception:
                pass
    return out


# ---------- 聚合入口 ----------

def discover_all(include_network: bool = True) -> list[TransportEndpoint]:
    """系统所有能看见的硬件口子——一行调用拿到全部。"""
    eps: list[TransportEndpoint] = []
    eps.extend(discover_serial())
    eps.extend(discover_usb())
    if include_network:
        eps.extend(scan_tcp())
    return eps
