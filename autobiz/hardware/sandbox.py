"""sandbox —— 自检责 + 越界拒绝。

任何命令下发前都经过这里检查：
  1. 语义是否在设备能力里？
  2. 数值是否在 range 内？
  3. 权限（某些设备是否只读 / 某些命令是否需要额外授权）？
  4. 频率（会不会把设备刷爆？poll_interval 下限？）

全部通过才放行。任一失败 → 抛 SandboxError，内核记 failure。
"""
from __future__ import annotations

from typing import Any


class SandboxError(Exception):
    """自检责拒绝：不能这么干。"""


def validate_command(descriptor: dict[str, Any],
                     command: str,
                     value: Any = None,
                     perm: str = "normal") -> None:
    """完整校验：语义 × 数值 × 权限。失败抛 SandboxError。"""
    caps = descriptor.get("capabilities", [])
    cap = next((c for c in caps if c.get("name") == command), None)
    if not cap:
        raise SandboxError(f"unknown command '{command}' (not in device capabilities)")

    ctype = cap.get("type", "read")
    if ctype == "read" and value is not None:
        raise SandboxError(f"'{command}' is read-only but value was provided")
    if ctype == "write" and value is None:
        raise SandboxError(f"'{command}' needs a value")

    # 数值边界
    if value is not None:
        rng = cap.get("range")
        if rng and len(rng) == 2:
            lo, hi = rng
            if lo is not None and hi is not None and isinstance(value, (int, float)):
                if value < lo or value > hi:
                    raise SandboxError(
                        f"'{command}' value {value} out of range [{lo}, {hi}]")
                # 整数范围 → value 必须是整（除非 range 本身是 float 归一化）
                if isinstance(lo, int) and isinstance(hi, int):
                    if isinstance(value, float) and not value.is_integer():
                        raise SandboxError(
                            f"'{command}' expects integer, got {value}")

    # 只读设备
    if perm == "read-only" and ctype in ("write", "rw"):
        raise SandboxError(f"device is read-only, '{command}' write denied")


def validate_capability(cap: dict[str, Any]) -> None:
    """自进化时的合法性检查（add_vocab 之前调用）。"""
    if not isinstance(cap, dict):
        raise SandboxError("capability must be dict")
    if "name" not in cap:
        raise SandboxError("capability missing 'name'")
    name = cap["name"]
    if not isinstance(name, str) or not name.isidentifier():
        raise SandboxError(f"capability name '{name}' invalid (must be identifier)")

    ctype = cap.get("type", "read")
    if ctype not in ("read", "write", "rw", "notify", "exec"):
        raise SandboxError(f"capability type '{ctype}' unknown")

    rng = cap.get("range")
    if rng is not None:
        if not isinstance(rng, list) or len(rng) != 2:
            raise SandboxError(f"range must be [lo, hi]")
        lo, hi = rng
        if lo is not None and hi is not None and lo > hi:
            raise SandboxError(f"range [{lo}, {hi}] invalid (lo > hi)")


def check_poll_interval(descriptor: dict[str, Any],
                         interval_ms: int) -> int:
    """保证 poll_interval 不低于设备允许的最小值。

    固件 defaults 里可以声明 min_poll_ms，没声明就给 100ms 硬下限。
    """
    defaults = descriptor.get("defaults") or {}
    min_poll = defaults.get("min_poll_ms", 100)
    if interval_ms < min_poll:
        return min_poll
    return interval_ms
