---
name: hak-hardware-runtime
description: Operate and extend the HAK v2.0 hardware stack in autobiz. Use when adding skills, composing pipelines, registering custom devices, debugging hardware API routes, or running smoke tests on Windows. Do not use for unrelated dashboard or database work.
---

# HAK Hardware Runtime Stack

三层架构：`skill_runtime` 负责单 skill 执行 + 全局 registry；`composer` 负责 Pipeline/Trigger/DataBus 编排；`skill_factory` 负责运行时动态注册。所有模块都围绕 `MockTransport` 产生真实数据，不需要硬件。

## 快速入口

```python
# 单 skill live read
from autobiz.hardware.skill_runtime import engine
engine().get("temperature_sensor").read("temperature")  # → 42.5

# 任务规划 + 执行
from autobiz.hardware.composer import compose_task
compose_task("patient monitoring")   # → Pipeline + Trigger 执行报告

# 运行时注册新 skill
from autobiz.hardware.skill_factory import register_skill
register_skill("my_fridge", "sensor", [
    {"name": "temp", "type": "read", "range": [-20, 10], "unit": "°C"},
    {"name": "defrost", "type": "exec"},
])
```

## 关键文件

| 文件 | 职责 |
|---|---|
| `autobiz/hardware/skill_runtime.py` | SkillRuntime(单 skill) + SkillEngine(120 skills registry) |
| `autobiz/hardware/composer.py` | DataBus + Pipeline(DAG) + Trigger(阈值推导) + Composer(任务规划) |
| `autobiz/hardware/skill_factory.py` | 运行时 register/unregister，静态 120 skills 写保护 |
| `autobiz/hardware/skills_library.py` | ALL_SKILLS dict + SKILL_NAMES + DEVICE_CLASS_MAP + count() |
| `autobiz/hardware/hds_library.json` | 120 skill 静态 descriptor |
| `autobiz/hardware/descriptor.py` | validate() + VALID_CLASSES(11 类) + VALID_CAP_TYPES(5 种) |
| `autobiz/hardware/transports/mock.py` | MockTransport —— 产生 midpoint + 高斯噪声的假数据 |
| `autobiz/dashboard.py` | 14 个硬件 API 端点 (GET/POST/DELETE) |

## 14 个 Dashboard 硬件 API 端点

所有端点都有 `try/except → HTTP 500` 降级，不会 crash。

```
GET    /api/hardware/skills                # 120 skills 总览 + custom_skills 列表
GET    /api/hardware/skills/{name}         # 单个 skill 详情 + 全部 live reads
GET    /api/hardware/skills/search/{kw}    # fuzzy capability 搜索 (top 20)
GET    /api/hardware/skills/compose?task=  # 任务 → skill bundle
GET    /api/hardware/skills/demo           # 28-skill 快速快照 (≈1-2s)
GET    /api/hardware/orchestrate?task=     # 完整 plan + pipeline + triggers
GET    /api/hardware/plan?task=            # Dry-run 规划草案
POST   /api/hardware/skills                # 运行时注册自定义 skill (201)
DELETE /api/hardware/skills/{name}         # 注销自定义 skill (400 拒绝删除静态)
```

## 高频坑点

### 1. Regex 路由会互相吞

`dashboard.py` 里 `/api/hardware/skills/{name}` 和 `/api/hardware/skills/demo` 是并列路径，**必须用 negative lookahead** 保留特殊端点。错误写法会让 `demo` 被 skill name regex 吞掉 → handler 返回 "unknown skill"。

```python
# ❌ 旧写法 — demo 被 skill regex 吞掉
re.match(r"^/api/hardware/skills/([a-z_]+)$", path)

# ✅ 正确写法
re.match(r"^/api/hardware/skills/(?!demo|search|compose|orchestrate|plan)([a-z0-9_]+)$", path)
```

### 2. Skill name 可以含数字

`imu_9axis` / `exoskeleton_4dof` / `ecg_sensor_2` — regex 的捕获组必须允许 `[a-z0-9_]`，不能只有 `[a-z_]`。

### 3. MockTransport 构造函数签名

```python
# ❌ 错误
MockTransport(endpoint="...", send=...)

# ✅ 正确 — 只有两个 kwarg
MockTransport(descriptor=dict, address="mock://hak-v2/...")
```

### 4. descriptor 必须用 hdp-1 协议

```python
validate({"protocol_version": "hdp-1", ...})   # ✅
validate({"protocol_version": "2.0", ...})     # ❌ DescriptorError
```

### 5. Windows dashboard 启动

`python -m autobiz.dashboard` 不会启动——模块没有 `__main__`。用 runner 脚本：

```powershell
# 后台启动
Start-Process python -ArgumentList "tools/_dash_runner.py" -WindowStyle Hidden

# 端口检查
netstat -ano | findstr ":8787.*LISTENING"

# 跑 smoke test
python tools/_e2e.py
```

详见 `references/windows-dashboard.md`。

## 扩展新 skill 的流程

1. 如果是**静态 skill**（要 commit 到 repo），直接往 `hds_library.json` 追加一条——`skills_library.py` 自动加载。
2. 如果是**运行时 skill**（临时测试，进程重启丢失），用 `skill_factory.register()`。
3. 两种都要先跑 self-test：`python -m autobiz.hardware.skill_runtime` 会读全部 skill 的首个 read-capability。
4. 如果增加了 dashboard 端点，写完后先加 `try/except → HTTP 500`，再跑 E2E smoke。

## 自测试入口

```powershell
# 模块级 (每个都有 __main__)
python -m autobiz.hardware.skill_runtime    # 120 skills + 5 live reads
python -m autobiz.hardware.composer          # Pipeline + Trigger + Composer
python -m autobiz.hardware.skill_factory     # 2 custom skills + 注销 + 保护

# Regex 路由单元测试 (12 cases)
python tools/_test_regex_routes.py

# HTTP 端到端 smoke (11 组全部通过)
python tools/_e2e.py
```

## 架构层次

```
hds_library.json (120 static descriptors, 静态只读)
        │
        ▼
skills_library.py  ←── skill_factory.py (运行时注入/注销)
  ALL_SKILLS dict         ├─ validate 做 schema 校验
  SKILL_NAMES             ├─ 静态 120 写保护 (_custom_skills set)
  DEVICE_CLASS_MAP        └─ invalidate SkillEngine cache
        │
        ▼
skill_runtime.py          ←── composer.py (编排层)
  SkillRuntime              Pipeline DAG (ThreadPoolExecutor 并发 fan-out)
    .read/write/execute     Trigger (双向 warn/crit 阈值自动推导)
    .simulate()             DataBus (pubsub key-value store)
  SkillEngine               Composer (task → plan → run → report)
    .get/.search/.compose
```

## 安全/写保护

`skill_factory.unregister()` 只能注销 `_custom_skills` 里的 skill。静态 120 个在 `skills_library.py` 导入时就写死了——`DELETE /api/hardware/skills/temperature_sensor` 会返回 400。

`SkillRuntime._runtime_cache` 是进程内单例——factory 注册/注销后要调 `_invalidate_runtime_cache()` 让下次 `get()` 重建。
