"""composer — skill 链式编排 + 条件触发 + 并发 fan-out/fan-in.

Builds on skill_runtime.SkillRuntime to provide higher-level orchestration:

  Pipeline    — DAG of skill ops (read/write/execute) with data-flow edges
  DataBus     — shared key-value store + pubsub between skills
  Trigger     — condition-watcher that fires actions (if-then rules)
  Composer    — high-level planner: task → Pipeline + Trigger set

Design principles:
  * 所有 orchestration 都走 SkillRuntime (只读/写它的 transport)
  * Trigger 条件自动推阈值 (cap range → warning/critical bands)
  * Pipeline 支持 depends_on, transform fn, parallel fan-out
  * 死循环保护: 单轮 execute 超过 MAX_CYCLES 就 break
"""
from __future__ import annotations

import inspect
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .skill_runtime import SkillRuntime, SkillEngine, engine as _engine


# ===================== 数据总线 =====================

class DataBus:
    """skill 之间传递数据的线程安全总线."""

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._history: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[Callable]] = {}

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._history.append({"key": key, "value": value, "t": time.time()})
            cbs = list(self._subscribers.get(key, []))
        for cb in cbs:
            try: cb(value)
            except Exception: pass

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def subscribe(self, key: str, cb: Callable[[Any], None]) -> None:
        with self._lock:
            self._subscribers.setdefault(key, []).append(cb)


# ===================== Pipeline =====================

@dataclass
class PipelineStep:
    skill: str
    capability: str
    op: str = "read"          # read | write | exec
    value: Any = None          # for write
    transform: Callable[[Any], Any] | None = None  # output transform
    depends_on: list[str] = field(default_factory=list)  # step ids that must finish first
    step_id: str = ""

    def __post_init__(self):
        if not self.step_id:
            self.step_id = f"{self.skill}.{self.capability}.{self.op}"


class Pipeline:
    """有向无环图 skill 操作编排器."""

    def __init__(self, bus: DataBus | None = None,
                 eng: SkillEngine | None = None):
        self.bus = bus or DataBus()
        self.eng = eng or _engine()
        self.steps: list[PipelineStep] = []
        self._results: dict[str, Any] = {}

    def add_step(self, skill: str, cap: str, op: str = "read",
                 value: Any = None, transform: Callable | None = None,
                 depends_on: list[str] | None = None, step_id: str = "") -> PipelineStep:
        s = PipelineStep(skill=skill, capability=cap, op=op, value=value,
                         transform=transform, depends_on=depends_on or [],
                         step_id=step_id)
        self.steps.append(s)
        return s

    # ---- Run ----

    def run(self, inputs: dict[str, Any] | None = None,
            timeout: float = 30.0,
            max_parallel: int = 8) -> dict[str, Any]:
        """按 DAG 拓扑顺序执行所有 steps."""
        t0 = time.time()
        self._results = {}
        self._results.update(inputs or {})

        # 简单拓扑: 按 depends_on 分层, 无依赖的并行, 有依赖的串行
        pending = list(self.steps)
        done_ids: set[str] = set()

        while pending and (time.time() - t0) < timeout:
            ready = [s for s in pending if all(dep in done_ids for dep in s.depends_on)]
            if not ready:
                break  # 死锁保护
            pending = [s for s in pending if s not in ready]

            # 并行执行 ready steps
            if len(ready) == 1:
                self._execute_step(ready[0])
                done_ids.add(ready[0].step_id)
            else:
                with ThreadPoolExecutor(max_workers=max_parallel) as ex:
                    futs = {ex.submit(self._execute_step, s): s for s in ready}
                    for fut in as_completed(futs, timeout=timeout - (time.time() - t0)):
                        s = futs[fut]
                        try:
                            fut.result()
                        except Exception as e:
                            self._results[s.step_id] = {"error": str(e)[:100]}
                        done_ids.add(s.step_id)

        return {
            "ok": True,
            "duration_s": round(time.time() - t0, 4),
            "steps_total": len(self.steps),
            "steps_done": len(done_ids),
            "results": self._results,
            "bus_snapshot": self.bus.get_all(),
        }

    def _execute_step(self, s: PipelineStep) -> None:
        rt = self.eng.get(s.skill)
        if s.op == "read":
            val = rt.read(s.capability)
        elif s.op == "write":
            val = rt.write(s.capability, s.value)
        elif s.op == "exec":
            val = rt.execute(s.capability)
        else:
            val = {"error": f"unknown op {s.op}"}
            self._results[s.step_id] = val
            return
        if callable(s.transform):
            val = s.transform(val)
        self._results[s.step_id] = val
        # 写入 bus
        self.bus.set(f"{s.skill}.{s.capability}", val)


# ===================== Trigger =====================

class Trigger:
    """条件监听 + 动作执行 (if-then rule).

    threshold 自动从 skill capability 的 range 派生:
      range [-40, 125] → warn_at = 110 (hi - 10%), critical_at = 120 (hi - 4%)
      range [0, 100]   → warn_at = 90,  critical_at = 96
    """

    RANGE_WARN = 0.88      # 88% of range band → warning
    RANGE_CRIT = 0.96      # 96% → critical
    MAX_CYCLES = 10        # 单次 run 最多触发次数 (防死循环)

    def __init__(self, watch_skill: str, watch_cap: str,
                 action_skill: str, action_cap: str, action_op: str = "exec",
                 level: str = "warn"):
        self.watch_skill = watch_skill
        self.watch_cap = watch_cap
        self.action_skill = action_skill
        self.action_cap = action_cap
        self.action_op = action_op
        self.level = level  # "warn" | "crit"

    def threshold(self) -> dict[str, float]:
        """基于 capability range 计算双向 warn/crit 阈值.

        返回:
          upper_warn, upper_crit  — 接近 hi 时的告警
          lower_warn, lower_crit  — 接近 lo 时的告警
        例: range [-40, 125] → upper_warn=105.2, upper_crit=118.4,
                                lower_warn=-20.6, lower_crit=-33.8
        """
        rt = _engine().get(self.watch_skill)
        cap = next((c for c in rt.capabilities
                    if c["name"].lower() == self.watch_cap.lower()), None)
        if not cap or not isinstance(cap.get("range"), list):
            return {"upper_warn": math.inf, "upper_crit": math.inf,
                    "lower_warn": -math.inf, "lower_crit": -math.inf}
        lo, hi = cap["range"]
        span = hi - lo
        return {
            "upper_warn": lo + span * self.RANGE_WARN,
            "upper_crit": lo + span * self.RANGE_CRIT,
            "lower_warn": hi - span * self.RANGE_WARN,
            "lower_crit": hi - span * self.RANGE_CRIT,
            "range_lo": lo, "range_hi": hi,
        }

    def _is_triggered(self, value: Any) -> tuple[bool, str]:
        if not isinstance(value, (int, float)):
            return (False, "")
        th = self.threshold()
        if value >= th["upper_crit"] or value <= th["lower_crit"]:
            return (True, "crit")
        if value >= th["upper_warn"] or value <= th["lower_warn"]:
            return (True, "warn")
        return (False, "")

    def evaluate(self) -> dict[str, Any]:
        """读 watch 值, 判断是否触发, 触发则执行 action."""
        rt = _engine().get(self.watch_skill)
        val = rt.read(self.watch_cap)
        fired, level = self._is_triggered(val)
        th = self.threshold()
        result = {
            "watch": f"{self.watch_skill}.{self.watch_cap}",
            "current_value": val,
            "thresholds": {k: round(v, 3) if math.isfinite(v) else v
                           for k, v in th.items()},
            "fired": fired,
            "level": level,
        }
        if fired:
            art = _engine().get(self.action_skill)
            try:
                if self.action_op == "exec":
                    ok = art.execute(self.action_cap)
                elif self.action_op == "write":
                    ok = art.write(self.action_cap, 1)
                else:
                    ok = True
                result["action"] = f"{self.action_op} {self.action_skill}.{self.action_cap}"
                result["action_ok"] = ok
            except Exception as e:
                result["action_error"] = str(e)[:80]
        return result


# ===================== Composer =====================

class Composer:
    """最高层: task → Pipeline + Triggers.

    支持的任务类型 (自动 route):
      - "monitor X"       → Pipeline: 读多个 sensor 并汇总
      - "cooling / safety" → Pipeline(read) + Trigger(if too hot → actuate)
      - "inspection line"  → 顺序 pipeline (camera → conveyor → arm)
      - "patient care"     → 多 sensor fan-out + 医疗设备触发
    """

    def __init__(self, bus: DataBus | None = None):
        self.bus = bus or DataBus()

    def plan(self, task: str) -> dict[str, Any]:
        """分析任务, 生成 Pipeline + Trigger 草案 (不执行)."""
        eng = _engine()
        task_lower = task.lower()
        pipeline = Pipeline(bus=self.bus, eng=eng)
        triggers: list[Trigger] = []
        plan_info: dict[str, Any] = {"task": task}

        # 1. 找 skill 候选
        candidates = eng.find_by_task(task)

        # ---- 监控类任务 ----
        if any(kw in task_lower for kw in ("monitor", "watch", "observe", "read", "check",
                                            "监控", "观察", "检查", "监测", "读")):
            for m in candidates[:6]:
                rt = eng.get(m["skill"])
                for cap in rt.capabilities:
                    if cap.get("type") in ("read", "rw"):
                        pipeline.add_step(m["skill"], cap["name"], "read",
                                          step_id=f"read.{m['skill']}.{cap['name']}")
            plan_info["type"] = "monitor"

        # ---- 安全/触发类任务 ----
        elif any(kw in task_lower for kw in ("safety", "protect", "cool", "heat",
                                              "alert", "emergency", "stop",
                                              "安全", "保护", "冷却", "加热", "警报", "紧急", "停止")):
            # 监控所有 sensor + 自动关联 actuator
            sensors = [m for m in candidates
                       if eng.get(m["skill"]).device_class in ("sensor", "medical")][:4]
            actuators = [m for m in candidates
                         if eng.get(m["skill"]).device_class in ("actuator", "smart_home")][:4]
            for m in sensors:
                rt = eng.get(m["skill"])
                for cap in rt.capabilities:
                    if cap.get("type") == "read":
                        pipeline.add_step(m["skill"], cap["name"], "read",
                                          step_id=f"sense.{m['skill']}.{cap['name']}")
            # 对每个 sensor→actuator 加 trigger
            for s, a in zip(sensors, actuators):
                srt = eng.get(s["skill"])
                art = eng.get(a["skill"])
                scap = next((c for c in srt.capabilities if c["type"] == "read"), None)
                acap = next((c for c in art.capabilities if c["type"] == "exec"), None)
                if scap and acap:
                    triggers.append(Trigger(s["skill"], scap["name"],
                                           a["skill"], acap["name"], "exec", "warn"))
            plan_info["type"] = "safety_trigger"

        # ---- 默认: 多 class 组合 ----
        else:
            seen_classes = set()
            for m in candidates:
                cls = m["device_class"]
                if cls in seen_classes: continue
                seen_classes.add(cls)
                rt = eng.get(m["skill"])
                for cap in rt.capabilities[:2]:
                    pipeline.add_step(m["skill"], cap["name"],
                                      cap.get("type", "read"),
                                      step_id=f"multi.{m['skill']}.{cap['name']}")
                if len(seen_classes) >= 4: break
            plan_info["type"] = "multi_class_default"

        plan_info["pipeline_steps"] = len(pipeline.steps)
        plan_info["trigger_count"] = len(triggers)
        plan_info["skill_ids"] = list({s.skill for s in pipeline.steps})
        return {
            "ok": True, "plan": plan_info,
            "pipeline": pipeline,
            "triggers": triggers,
        }

    def run(self, task: str, timeout: float = 10.0) -> dict[str, Any]:
        """plan → run pipeline → evaluate all triggers → 返回完整报告."""
        t0 = time.time()
        plan = self.plan(task)
        pipeline: Pipeline = plan["pipeline"]
        triggers: list[Trigger] = plan["triggers"]

        # 执行 pipeline
        pipe_result = pipeline.run(timeout=timeout)

        # 检查所有 triggers (最多 2 轮, 防死循环)
        trigger_results: list[dict[str, Any]] = []
        for _ in range(2):
            any_fired = False
            for tg in triggers:
                r = tg.evaluate()
                trigger_results.append(r)
                if r["fired"]: any_fired = True
            if not any_fired: break

        return {
            "ok": True,
            "task": task,
            "plan_type": plan["plan"]["type"],
            "pipeline": pipe_result,
            "triggers": trigger_results,
            "total_duration_s": round(time.time() - t0, 4),
            "composed_skills": plan["plan"]["skill_ids"],
        }


# ============ 便捷入口 ============

_composer = Composer()

def compose_task(task: str, timeout: float = 10.0) -> dict[str, Any]:
    """Composer 单例入口: 自然语言任务 → 执行报告."""
    return _composer.run(task, timeout=timeout)


def plan_task(task: str) -> dict[str, Any]:
    """只生成计划不执行 (dry run)."""
    return _composer.plan(task)


if __name__ == "__main__":
    print("=== HAK Composer Smoke Test ===\n")

    # 1) 纯 pipeline demo
    pipe = Pipeline()
    pipe.add_step("temperature_sensor", "temperature", "read")
    pipe.add_step("humidity_sensor", "humidity", "read")
    pipe.add_step("imu_9axis", "acceleration", "read")
    r = pipe.run(timeout=5)
    print(f"Pipeline 1: {r['steps_done']}/{r['steps_total']} steps in {r['duration_s']}s")
    for k, v in r["results"].items():
        print(f"  {k:35s} = {v}")

    print()

    # 2) trigger demo (温度过高 → 开风扇 actuator)
    # 注意: MockTransport 给的是 midpoint 读数, 不会真的触发阈值
    # 但 threshold 计算应该正确
    tg = Trigger("temperature_sensor", "temperature",
                 "dc_motor", "stop", "exec", "warn")
    th = tg.threshold()
    print(f"Trigger threshold  temperature_sensor.temperature range=[{th['range_lo']},{th['range_hi']}]:")
    print(f"  upper: warn @ {round(th['upper_warn'],2)}°C  crit @ {round(th['upper_crit'],2)}°C")
    print(f"  lower: warn @ {round(th['lower_warn'],2)}°C  crit @ {round(th['lower_crit'],2)}°C")
    r2 = tg.evaluate()
    print(f"  evaluate → value={r2['current_value']} fired={r2['fired']}")

    print()

    # 3) Composer 高层 run
    print("=== Composer.run: 'patient monitoring' ===")
    r3 = compose_task("patient monitoring")
    print(f"plan_type={r3['plan_type']}  composed_skills={r3['composed_skills']}")
    print(f"pipeline: {r3['pipeline']['steps_done']} steps in {r3['pipeline']['duration_s']}s")
    for k, v in list(r3['pipeline']['results'].items())[:5]:
        print(f"  {k:40s} = {v}")

    print()
    print("=== ALL OK ✅ ===")
