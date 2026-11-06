"""skill_factory — 运行时动态注册/注销自定义 skill.

不修改 hds_library.json (保持静态库纯净), 只在运行时注入到
ALL_SKILLS + DEVICE_CLASS_MAP + SKILL_NAMES, 并刷新 SkillRuntime 缓存.

典型用例:
  POST /api/hardware/skills {
    "name": "my_fridge_temp",
    "device_class": "sensor",
    "vendor_id": "my_home_v1",
    "capabilities": [
      {"name": "temperature", "type": "read", "range": [-20, 10], "unit": "°C"},
      {"name": "set_temp",    "type": "write", "range": [-15, 5],  "unit": "°C"},
      {"name": "defrost",     "type": "exec"}
    ]
  }
  → 注册成功, SkillEngine.get("my_fridge_temp") 即可用
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from .descriptor import VALID_CLASSES, VALID_CAP_TYPES, validate
from .skills_library import ALL_SKILLS, SKILL_NAMES, DEVICE_CLASS_MAP

# 运行时添加的 skill 名称集合 (不持久化, 进程重启即清除)
_custom_skills: set[str] = set()
_factory_ts: dict[str, float] = {}   # 注册时间戳


class SkillFactory:
    """运行时 skill 注册/注销."""

    @staticmethod
    def register(name: str, device_class: str,
                 capabilities: list[dict[str, Any]],
                 vendor_id: str = "custom_v1",
                 serial_number: str | None = None,
                 **extra: Any) -> dict[str, Any]:
        """注册一个新的 skill, 返回 validate 后的 descriptor.

        Raises ValueError: name 已存在 / device_class 不合法 /
                         capabilities 格式错误.
        """
        name = name.strip()
        if not name.replace("_", "").isalnum():
            raise ValueError(f"invalid skill name: {name!r} (use [a-z0-9_])")
        if name in ALL_SKILLS:
            raise ValueError(f"skill {name!r} already exists (use unregister first)")
        if device_class not in VALID_CLASSES:
            raise ValueError(f"invalid device_class {device_class!r}; valid: {sorted(VALID_CLASSES)}")

        # 验证 capabilities
        SkillFactory._validate_capabilities(capabilities)

        descriptor = {
            "protocol_version": "hdp-1",
            "vendor_id": vendor_id,
            "device_class": device_class,
            "serial_number": serial_number or f"SN-CUSTOM-{uuid.uuid4().hex[:8].upper()}",
            "capabilities": capabilities,
            "factory_registered": True,
            "registered_at": time.time(),
        }

        # 调用 validate (会做完整 schema 校验 + 默认值填充)
        validated = validate(descriptor)

        # 运行时注入
        ALL_SKILLS[name] = validated
        SKILL_NAMES.append(name)
        DEVICE_CLASS_MAP.setdefault(device_class, []).append(name)
        _custom_skills.add(name)
        _factory_ts[name] = time.time()

        # 刷新 SkillRuntime 缓存 (下次 get 会重建)
        SkillFactory._invalidate_runtime_cache()

        return validated

    @staticmethod
    def unregister(name: str) -> bool:
        """注销一个 skill. 只能注销运行时注册的 (custom) skill.

        保护: 不能删除 hds_library.json 里的 120 个静态 skill.
        """
        if name not in _custom_skills:
            return False
        desc = ALL_SKILLS.pop(name, None)
        if desc:
            cls = desc.get("device_class", "")
            if cls in DEVICE_CLASS_MAP and name in DEVICE_CLASS_MAP[cls]:
                DEVICE_CLASS_MAP[cls].remove(name)
        if name in SKILL_NAMES:
            SKILL_NAMES.remove(name)
        _custom_skills.discard(name)
        _factory_ts.pop(name, None)
        SkillFactory._invalidate_runtime_cache()
        return True

    @staticmethod
    def list_custom() -> list[dict[str, Any]]:
        """列出所有运行时注册的 skill (带注册时间和简要信息)."""
        out = []
        for name in sorted(_custom_skills):
            desc = ALL_SKILLS.get(name)
            if not desc: continue
            out.append({
                "name": name,
                "device_class": desc["device_class"],
                "capability_count": len(desc["capabilities"]),
                "capability_names": [c["name"] for c in desc["capabilities"]],
                "registered_at": _factory_ts.get(name, 0),
            })
        return out

    @staticmethod
    def is_custom(name: str) -> bool:
        return name in _custom_skills

    @staticmethod
    def count() -> int:
        return len(_custom_skills)

    # ---- 内部 ----

    @staticmethod
    def _validate_capabilities(caps: list[dict[str, Any]]) -> None:
        if not caps:
            raise ValueError("must provide at least 1 capability")
        names_seen: set[str] = set()
        for i, cap in enumerate(caps):
            if not isinstance(cap, dict):
                raise ValueError(f"cap[{i}] must be dict, got {type(cap).__name__}")
            n = cap.get("name", "").strip()
            if not n:
                raise ValueError(f"cap[{i}] missing 'name'")
            if n in names_seen:
                raise ValueError(f"duplicate capability name: {n}")
            names_seen.add(n)
            t = cap.get("type", "read")
            if t not in VALID_CAP_TYPES:
                raise ValueError(f"cap {n!r} has invalid type {t!r}; valid: {sorted(VALID_CAP_TYPES)}")
            if t in ("read", "rw", "write") and "range" not in cap:
                raise ValueError(f"cap {n!r} ({t}) must include 'range': [lo, hi]")

    @staticmethod
    def _invalidate_runtime_cache() -> None:
        """清空 SkillEngine 单例的 runtime_cache, 让下次 get 重建."""
        try:
            from .skill_runtime import engine as _eng
            _eng()._runtime_cache.clear()
        except Exception:
            pass


# ============ 便捷入口 ============

def register_skill(name: str, device_class: str, capabilities: list[dict],
                   vendor_id: str = "custom_v1", **extra) -> dict[str, Any]:
    return SkillFactory.register(name, device_class, capabilities, vendor_id=vendor_id, **extra)

def unregister_skill(name: str) -> bool:
    return SkillFactory.unregister(name)

def custom_skills() -> list[dict[str, Any]]:
    return SkillFactory.list_custom()


if __name__ == "__main__":
    print("=== SkillFactory Smoke Test ===\n")

    # 注册一个模拟冰箱
    desc = register_skill(
        "test_fridge_temp", "sensor",
        capabilities=[
            {"name": "temperature", "type": "read", "range": [-20, 10], "unit": "°C"},
            {"name": "set_temp",    "type": "write", "range": [-15, 5],  "unit": "°C"},
            {"name": "defrost",     "type": "exec"},
        ],
        vendor_id="test_v1",
    )
    print(f"Registered: {desc['serial_number']}")
    print(f"  caps: {[c['name'] for c in desc['capabilities']]}")

    # 验证 SkillRuntime 能 get 到 + 读值
    from .skill_runtime import engine
    eng = engine()
    rt = eng.get("test_fridge_temp")
    val = rt.read("temperature")
    print(f"  live read: temperature = {val}°C")
    print(f"  set_temp write ok: {rt.write('set_temp', -5)}")
    print(f"  defrost exec ok: {rt.execute('defrost')}")

    # 检查 custom_skills
    cs = custom_skills()
    print(f"\nCustom skills: {len(cs)} — {[s['name'] for s in cs]}")

    # 再注册一个 actuator
    desc2 = register_skill(
        "test_vacuum_robot", "actuator",
        capabilities=[
            {"name": "battery",   "type": "read", "range": [0, 100], "unit": "%"},
            {"name": "speed",    "type": "write", "range": [0, 5],   "unit": "m/s"},
            {"name": "start",    "type": "exec"},
            {"name": "stop",     "type": "exec"},
        ],
    )
    rt2 = eng.get("test_vacuum_robot")
    print(f"\nRegistered actuator: test_vacuum_robot")
    print(f"  battery = {rt2.read('battery')}%")

    # 注销 + 验证静态 skill 没被影响
    print(f"\nBefore unregister: total={eng.count()}")
    unregister_skill("test_fridge_temp")
    unregister_skill("test_vacuum_robot")
    print(f"After unregister:  total={eng.count()}")
    # 验证静态 temperature_sensor 还在
    print(f"  temperature_sensor still ok: "
          f"{eng.get('temperature_sensor').read('temperature')}")

    print(f"\n=== ALL OK ✅ ===")
