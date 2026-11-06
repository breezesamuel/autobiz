"""HDSL — Hardware Device Skill Library — 100 skill 定义 (自动生成)."""
from __future__ import annotations
import json, os, uuid
from typing import Any

_HDS_PATH = os.path.join(os.path.dirname(__file__), "hds_library.json")

def _load():
    with open(_HDS_PATH, encoding="utf-8") as _f:
        return json.load(_f)

ALL_SKILLS: dict[str, dict[str, Any]] = _load()
SKILL_NAMES: list[str] = sorted(ALL_SKILLS.keys())

DEVICE_CLASS_MAP: dict[str, list[str]] = {}
for _n, _d in ALL_SKILLS.items():
    DEVICE_CLASS_MAP.setdefault(_d["device_class"], []).append(_n)

def count() -> int:
    return len(ALL_SKILLS)

def classes() -> list[str]:
    return sorted(DEVICE_CLASS_MAP.keys())

def by_class(dev_class: str) -> list[str]:
    return DEVICE_CLASS_MAP.get(dev_class, [])

def make_descriptor(skill_name: str, sn: str | None = None) -> dict[str, Any]:
    """从库中取一个 skill，生成合法 descriptor (带唯一 SN)."""
    if skill_name not in ALL_SKILLS:
        raise KeyError(f"unknown skill: {skill_name!r} — available: {SKILL_NAMES}")
    import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from .descriptor import validate
    base = dict(ALL_SKILLS[skill_name])
    if sn is None:
        sn = f"SN-{skill_name.upper()}-{uuid.uuid4().hex[:8].upper()}"
    base["serial_number"] = sn
    return validate(base)

def register_all_with_hak() -> dict[str, Any]:
    """把全部 100 skill 注册到 HAK v2.0 registry (mock transport)."""
    from .transports.mock import MockTransport
    from .descriptor import validate
    from .registry import Registry
    reg = Registry()
    registered = []
    failed = []
    for name, raw in ALL_SKILLS.items():
        try:
            desc = validate(dict(raw))
            address = f"mock://hak-v2/indicator"
            transport = MockTransport(descriptor=desc, address=address)
            entry = reg.register(desc, transport=transport, address=address)
            registered.append(name)
        except Exception as e:
            failed.append((name, str(e)[:60]))
    return {
        "total": len(ALL_SKILLS),
        "registered": len(registered),
        "failed": failed,
        "entries": reg.count(),
    }

if __name__ == "__main__":
    print(f"Total: {count()} skills")
    for c, names in sorted(DEVICE_CLASS_MAP.items()):
        print(f"  {c}: {len(names)} — {', '.join(names[:3])}...")
