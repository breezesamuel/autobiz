"""skill_runtime — 让 120 HAK skills 变成可执行 runtime agent.

核心目标: 硬件全自动接入 — 发现 → 握手 → 注册 → 读写控制 → 数据上报

DataBus (本模块内置): 所有 skill 的 MockTransport 共享同一个状态空间,
                     写 A 会被 B 读到, 不再是 120 个各自独立的模拟器.
"""
from __future__ import annotations

import json
import math
import random
import time
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from .descriptor import validate
from .transports.mock import MockTransport
from .skills_library import (
    ALL_SKILLS, SKILL_NAMES, DEVICE_CLASS_MAP, count as skill_count,
)

_HDS_PATH = Path(__file__).parent / "hds_library.json"


# ============= DataBus — 所有 skill 共享的状态空间 =============

class DataBus:
    """线程安全的 pub/sub + key-value 状态总线.

    - set(key, val) — 写值, 通知所有订阅者
    - get(key) → val — 读值, 没写过返回 None
    - subscribe(key_prefix, callback) — 订阅变化
    """
    def __init__(self):
        self._data: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._subs: dict[str, list[callable]] = {}

    def set(self, key: str, val: Any) -> None:
        with self._lock:
            self._data[key] = val
            # 通知订阅者
            subs = self._subs.get(key, [])
        for cb in subs:
            try: cb(key, val)
            except Exception: pass

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self._data.get(key)

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def subscribe(self, key_prefix: str, callback) -> None:
        """订阅以 key_prefix 开头的所有 key 变化."""
        with self._lock:
            self._subs.setdefault(key_prefix, []).append(callback)


# 模块级全局单例 — 所有 MockTransport / SkillRuntime 共享这一个 bus
_global_bus: DataBus | None = None
_global_bus_lock = threading.Lock()

def get_bus() -> DataBus:
    global _global_bus
    if _global_bus is None:
        with _global_bus_lock:
            if _global_bus is None:
                _global_bus = DataBus()
    return _global_bus

def reset_bus() -> None:
    global _global_bus
    _global_bus = None


class SkillRuntime:
    """一个 skill 的完整运行时: descriptor + transport + 执行 API.

    所有 SkillRuntime 共享同一个 DataBus — 写一个 skill 会被另一个读到.
    """

    def __init__(self, skill_name: str, bus=None):
        if skill_name not in ALL_SKILLS:
            raise KeyError(f"unknown skill: {skill_name!r}")
        raw = ALL_SKILLS[skill_name]
        self.name = skill_name
        self.descriptor = validate(dict(raw))
        self._bus = bus if bus is not None else get_bus()
        self.transport = MockTransport(
            descriptor=self.descriptor,
            address=f"mock://hak-v2/{skill_name.lower()}",
            bus=self._bus,
        )
        self._running = True
        self._lock = threading.Lock()

    # ---- 基础查询 ----

    @property
    def device_class(self) -> str:
        return self.descriptor["device_class"]

    @property
    def capabilities(self) -> list[dict[str, Any]]:
        return self.descriptor["capabilities"]

    @property
    def capability_names(self) -> list[str]:
        return [c["name"] for c in self.capabilities]

    def supports(self, capability_name: str) -> bool:
        return any(c["name"].lower() == capability_name.lower()
                   for c in self.capabilities)

    # ---- 高层执行 API ----

    def read(self, cap_name: str) -> Any:
        """读一个 capability, 返回值 (按 unit 自动转换).

        能力不存在 / transport 超时 → 返回 None (不抛异常).
        """
        with self._lock:
            self.transport.write(f"READ {cap_name}")
            try:
                resp = self.transport.read_line(timeout=5.0)
            except Exception:
                return None
            if resp is None:
                return None
            if resp.startswith("ERR"):
                return None
            cap = next((c for c in self.capabilities
                        if c["name"].lower() == cap_name.lower()), None)
            return self._parse_resp(resp, cap)

    def write(self, cap_name: str, value: Any) -> bool:
        """写一个 capability. 能力不存在 / transport 失败 → False."""
        with self._lock:
            self.transport.write(f"WRITE {cap_name} {value}")
            try:
                resp = self.transport.read_line(timeout=5.0)
            except Exception:
                return False
            return resp == "OK"

    def execute(self, cmd_name: str) -> bool:
        """执行一个 exec capability (无参数). 能力不存在 → False."""
        with self._lock:
            self.transport.write(f"EXEC {cmd_name}")
            try:
                resp = self.transport.read_line(timeout=5.0)
            except Exception:
                return False
            return resp == "OK"

    # ---- 时间序列模拟 ----

    def simulate(self, seconds: float = 5.0, interval: float = 0.1) -> list[dict[str, Any]]:
        """模拟一段时间的 sensor 读数 (带自然波动)."""
        readings = []
        t0 = time.time()
        # 确定要读哪些 read-capability
        read_caps = [c for c in self.capabilities if c.get("type") in ("read", "rw")]
        while time.time() - t0 < seconds:
            point = {"t": round(time.time() - t0, 3)}
            for cap in read_caps:
                point[cap["name"]] = self._jittered_read(cap)
            readings.append(point)
            time.sleep(interval)
        return readings

    # ---- 内部 ----

    def _jittered_read(self, cap: dict[str, Any]) -> Any:
        rng = cap.get("range") or [-40, 85]
        lo, hi = rng[0], rng[1]
        mid = (lo + hi) / 2
        span = hi - lo
        noise = random.gauss(0, span * 0.02)  # 2% 高斯噪声
        val = mid + noise
        if isinstance(lo, int) and isinstance(hi, int):
            val = max(lo, min(hi, int(round(val))))
        else:
            val = round(max(lo, min(hi, val)), 4)
        return val

    @staticmethod
    def _parse_resp(resp: str, cap: dict[str, Any] | None) -> Any:
        try:
            v = float(resp)
            if cap and isinstance(cap.get("range"), list):
                lo = cap["range"][0]
                if isinstance(lo, int) and v == int(v):
                    return int(v)
            return v
        except ValueError:
            return resp

    def shutdown(self) -> None:
        self._running = False
        self.transport.close()


# ========== 引擎: 加载全部 skills ==========

# ========== 场景路由 ==========

# 每个 device_class 对应一组场景关键词 — compose_task 先按场景路由
# 这解决了 "patient monitoring" 被错误匹配到 smart_home 的问题
SCENARIO_CLASS_MAP: dict[str, list[str]] = {
    # 场景关键词 (小写) → device_class 列表
    "patient":      ["medical", "sensor"],
    "hospital":     ["medical", "sensor"],
    "medical":      ["medical", "sensor"],
    "clinic":       ["medical"],
    "health":       ["medical", "sensor"],
    "monitor":      ["sensor", "medical", "energy"],
    "监控":          ["sensor", "medical", "energy"],
    "安全":          ["sensor", "actuator", "smart_home"],
    "汽车":          ["vehicle", "sensor", "energy"],
    "vehicle":      ["vehicle", "energy"],
    "car":          ["vehicle", "energy"],
    "ev":           ["vehicle", "energy"],
    "home":         ["smart_home", "actuator", "sensor"],
    "smart":        ["smart_home", "gateway"],
    "factory":      ["industrial", "actuator", "sensor"],
    "industrial":   ["industrial", "actuator", "sensor"],
    "产线":          ["industrial", "actuator"],
    "robot":        ["robot", "actuator"],
    "机械臂":        ["robot"],
    "arm":          ["robot"],
    "exoskeleton":  ["exoskeleton", "sensor"],
    "industrial iot": ["industrial", "gateway", "sensor"],
    "iot":          ["gateway", "sensor", "smart_home"],
    "energy":       ["energy", "sensor"],
    "power":        ["energy", "sensor", "actuator"],
    "cooling":      ["actuator", "sensor"],
    "heat":         ["actuator", "sensor"],
    "cool":         ["actuator", "sensor"],
    "safety":       ["sensor", "actuator", "smart_home"],
    "inspection":   ["sensor", "camera_1080p"],
    "agv":          ["robot", "vehicle"],
    "drone":        ["robot", "sensor"],
}


class SkillEngine:
    """全部 120 skills 的运行时引擎."""

    def __init__(self):
        self._runtime_cache: dict[str, SkillRuntime] = {}

    @property
    def skill_names(self) -> list[str]:
        return SKILL_NAMES

    def get(self, skill_name: str) -> SkillRuntime:
        if skill_name not in self._runtime_cache:
            self._runtime_cache[skill_name] = SkillRuntime(skill_name)
        return self._runtime_cache[skill_name]

    def list_by_class(self) -> dict[str, list[str]]:
        return DEVICE_CLASS_MAP

    def count(self) -> int:
        return skill_count()

    # ---- 发现: 按 capability 关键词搜 skill ----

    def find_by_capability(self, keyword: str, fuzzy: bool = True) -> list[dict[str, Any]]:
        """按 capability 关键词找匹配的 skills."""
        kw = keyword.lower().strip()
        if not kw:
            return []
        results = []
        for name in SKILL_NAMES:
            raw = ALL_SKILLS[name]
            caps = [c["name"] for c in raw["capabilities"]]
            class_match = raw["device_class"]
            # 精确匹配 + fuzzy 关键词包含 (只允许 keyword 包含在 capability 名里, 禁止反向)
            # 防止 "on" in "monitoring" 这种子串误匹配
            score = 0
            matched_caps = []
            for c in caps:
                cl = c.lower()
                if cl == kw:
                    score += 100; matched_caps.append(c)
                elif kw in cl and len(kw) >= 4:
                    # keyword 作为 capability 的子串 (kw 至少 4 字符防误匹配)
                    score += 30; matched_caps.append(c)
            if class_match == kw:
                score += 80
            if score > 0:
                results.append({
                    "skill": name,
                    "device_class": class_match,
                    "matched_caps": matched_caps,
                    "score": score,
                    "total_caps": len(caps),
                })
        results.sort(key=lambda r: -r["score"])
        return results

    def find_by_task(self, task: str) -> list[dict[str, Any]]:
        """按自然语言任务描述找合适的 skill (场景路由优先 + capability 打分).

        流程:
          1. 场景路由 — 先看 task 里的词匹配哪个 SCENARIO_CLASS_MAP key
          2. 拿到目标 device_class 白名单
          3. 在白名单内做 capability fuzzy 匹配打分
          4. 没命中任何场景 → 回退到全量 capability 匹配
        """
        task_lower = task.lower()
        target_classes: set[str] = set()

        # Step 1: 场景路由
        for scenario_kw, classes in SCENARIO_CLASS_MAP.items():
            if scenario_kw in task_lower:
                target_classes.update(classes)

        # Step 2: 生成 capability 关键词
        keywords = []
        for seg in task_lower.split():
            if len(seg) > 2 and any('a' <= c <= 'z' for c in seg):
                keywords.append(seg)
            for n in (2, 3):
                for i in range(len(seg) - n + 1):
                    gram = seg[i:i+n]
                    if gram.strip() and any('\u4e00' <= c <= '\u9fff' for c in gram):
                        keywords.append(gram)
        keywords = list(dict.fromkeys(keywords))[:30]

        # Step 3: 打分 — 在 target_classes 白名单内, 或无白名单时全量
        scores: Counter[str] = Counter()
        matched_caps_map: dict[str, list[str]] = {}
        for name in SKILL_NAMES:
            raw = ALL_SKILLS[name]
            cls = raw["device_class"]
            # 白名单过滤 — 只挑场景匹配的 class
            if target_classes and cls not in target_classes:
                continue
            # class 匹配加分
            score = 0
            if cls in target_classes:
                score += 50  # 场景命中的 class 给基础分
            # capability fuzzy
            for c in raw["capabilities"]:
                cl = c["name"].lower()
                for kw in keywords:
                    if cl == kw:
                        score += 100
                        matched_caps_map.setdefault(name, []).append(c["name"])
                    elif kw in cl and len(kw) >= 4:
                        score += 30
                        matched_caps_map.setdefault(name, []).append(c["name"])
            if score > 0:
                scores[name] += score

        # Step 4: 结果 — 按分数排序, top 20
        results = []
        for name, score in scores.most_common(20):
            raw = ALL_SKILLS[name]
            results.append({
                "skill": name,
                "device_class": raw["device_class"],
                "score": score,
                "matched_caps": list(set(matched_caps_map.get(name, []))),
            })

        # 如果场景路由没找到任何结果, 回退到全量 capability 匹配
        if not results and target_classes:
            for kw in keywords:
                for m in self.find_by_capability(kw, fuzzy=True):
                    scores[m["skill"]] += m["score"]
                    matched_caps_map.setdefault(m["skill"], []).extend(m["matched_caps"])
            for name, score in scores.most_common(20):
                raw = ALL_SKILLS[name]
                results.append({
                    "skill": name,
                    "device_class": raw["device_class"],
                    "score": score,
                    "matched_caps": list(set(matched_caps_map.get(name, []))),
                })

        return results

    # ---- Skill 组合 ----

    def compose(self, task: str) -> dict[str, Any]:
        """自动组合 skills 完成一个任务."""
        matches = self.find_by_task(task)
        if not matches:
            return {"ok": False, "error": f"no skills match task: {task}"}
        # 选 top-N 不同 class 的 skill 组合
        picked: list[dict[str, Any]] = []
        seen_classes = set()
        for m in matches:
            if m["device_class"] not in seen_classes:
                picked.append(m)
                seen_classes.add(m["device_class"])
            if len(picked) >= 4:
                break
        # 快速验证每个能跑
        verified = []
        for m in picked:
            try:
                rt = self.get(m["skill"])
                first_cap = rt.capabilities[0]
                val = rt.read(first_cap["name"])
                verified.append({
                    "skill": m["skill"],
                    "device_class": m["device_class"],
                    "read_test": {first_cap["name"]: val},
                    "capabilities": [c["name"] for c in rt.capabilities],
                })
            except Exception as e:
                verified.append({
                    "skill": m["skill"], "error": str(e)[:80],
                })
        return {
            "ok": True,
            "task": task,
            "skills_count": len(verified),
            "skills": verified,
        }

    # ---- 批量模拟演示 ----

    def demo(self, seconds: float = 2.0) -> dict[str, Any]:
        """快速模拟全部 120 skills 的 2s 数据 (演示用)."""
        demo_skills = [
            "temperature_sensor", "imu_9axis", "radar_24ghz", "camera_1080p",
            "dc_motor", "servo_motor", "led_rgb", "relay_module",
            "robotic_arm_6dof", "mobile_base_diff", "quadruped_robot", "drone_quadrotor",
            "exoskeleton_4dof", "humanoid_torso",
            "smart_lock_door", "thermostat", "vacuum",
            "cnc_controller", "industrial_robot_6axis", "3d_printer_fdm",
            "ecg_monitor", "infusion_pump",
            "solar_inverter", "ev_charger_l2", "battery_management",
            "car_ecu_obd", "adas_camera", "vehicle_lidar",
        ]
        out = {}
        for name in demo_skills:
            try:
                rt = self.get(name)
                # 读每个 read-capability 一次
                snapshot = {}
                for cap in rt.capabilities:
                    if cap.get("type") in ("read", "rw"):
                        try:
                            snapshot[cap["name"]] = rt.read(cap["name"])
                        except: pass
                out[name] = {
                    "device_class": rt.device_class,
                    "snapshot": snapshot,
                }
            except Exception as e:
                out[name] = {"error": str(e)[:60]}
        return {"ok": True, "skills": out, "count": len(out)}


# ============ 单例 ============

_engine: SkillEngine | None = None

def engine() -> SkillEngine:
    global _engine
    if _engine is None:
        _engine = SkillEngine()
    return _engine


def search(keyword: str) -> list[dict[str, Any]]:
    return engine().find_by_capability(keyword)

def read(skill_name: str, cap: str) -> Any:
    return engine().get(skill_name).read(cap)

def demo(seconds: float = 2.0) -> dict[str, Any]:
    return engine().demo(seconds)


if __name__ == "__main__":
    eng = engine()
    print(f"=== HAK v2.0 Skill Engine ===\n")
    print(f"Total skills: {eng.count()}")
    for cls, names in eng.list_by_class().items():
        print(f"  {cls:15s}: {len(names):>3d} — {', '.join(names[:3])}...")

    # 测试几个
    print(f"\n=== Read Tests ===")
    for sn, cap in [
        ("imu_9axis", "acceleration"),
        ("temperature_sensor", "temperature"),
        ("radar_24ghz", "target_distance"),
        ("exoskeleton_4dof", "joint_pos"),
        ("ecg_monitor", "heart_rate"),
    ]:
        try:
            v = eng.get(sn).read(cap)
            print(f"  {sn}.{cap} = {v}")
        except Exception as e:
            print(f"  {sn}.{cap} = ERR {e}")

    # 搜索
    print(f"\n=== Search: 'heart' ===")
    for m in eng.find_by_capability("heart")[:5]:
        print(f"  {m['skill']:20s} ({m['device_class']:12s}) score={m['score']} caps={m['matched_caps']}")

    # 任务组合
    print(f"\n=== Compose: 'patient monitoring' ===")
    c = eng.compose("patient monitoring")
    for s in c.get("skills", []):
        print(f"  {s.get('skill', '?'):25s} class={s.get('device_class','?')} snapshot={s.get('read_test',{})}")

    print(f"\n=== ALL OK ✅ ===")
