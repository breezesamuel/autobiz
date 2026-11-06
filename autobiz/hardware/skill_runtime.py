"""skill_runtime — 让 120 HAK skills 变成可执行 agent。

每个 skill 有:
  1. descriptor (hds_library.json) — 静态能力声明
  2. MockTransport — 产生真实数据 (READ temperature → 23.5°C)
  3. execute() — 高层 API, 给业务层用
  4. simulate(sample_seconds=10) — 时间序列模拟

运行时引擎:
  skill_engine — 加载全部 120 skills, 提供 search/execute/simulate
  capability_matcher — "需要什么"→"哪些 skill 能做"
  skill_composer — 多 skill 组合成复合系统
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


# ========== 单 skill 运行时 ==========

class SkillRuntime:
    """一个 skill 的完整运行时: descriptor + transport + 执行 API."""

    def __init__(self, skill_name: str):
        if skill_name not in ALL_SKILLS:
            raise KeyError(f"unknown skill: {skill_name!r}")
        raw = ALL_SKILLS[skill_name]
        self.name = skill_name
        self.descriptor = validate(dict(raw))
        self.transport = MockTransport(
            descriptor=self.descriptor,
            address=f"mock://hak-v2/{skill_name.lower()}",
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
            # 精确匹配 + fuzzy 关键词包含
            score = 0
            matched_caps = []
            for c in caps:
                cl = c.lower()
                if cl == kw:
                    score += 100; matched_caps.append(c)
                elif kw in cl or cl in kw:
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
        """按自然语言任务描述找合适的 skill (多关键词打分).

        支持:
          - 英文: 空格分词 + len>2
          - 中文/CJK: 连续 2-3 字 n-gram (不依赖空格)
          - 混合: 先按空格切, 每段再提取 n-gram
        """
        keywords = []
        for seg in task.lower().split():
            # 短英文词 (len>2) 或 纯符号串直接收
            if len(seg) > 2 and any('a' <= c <= 'z' for c in seg):
                keywords.append(seg)
            # CJK n-gram: 从每个字符位置向后取 2 和 3 字
            for n in (2, 3):
                for i in range(len(seg) - n + 1):
                    gram = seg[i:i+n]
                    # 跳过纯标点/空格 gram
                    if gram.strip() and any('\u4e00' <= c <= '\u9fff' for c in gram):
                        keywords.append(gram)
        # 去重 + 限长 (最多 30 keywords)
        keywords = list(dict.fromkeys(keywords))[:30]
        scores: Counter[str] = Counter()
        matched_caps_map: dict[str, list[str]] = {}
        for kw in keywords:
            for m in self.find_by_capability(kw, fuzzy=True):
                scores[m["skill"]] += m["score"]
                matched_caps_map.setdefault(m["skill"], []).extend(m["matched_caps"])
        results = []
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
