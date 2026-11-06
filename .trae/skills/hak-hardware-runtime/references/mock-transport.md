# MockTransport 使用手册

## 核心职责

不依赖任何真实硬件，用内存 buffer 模拟 read/write/exec 命令，让 120 个 HAK skill 在开发时就能"跑起来"并产生合理数值。

## 构造函数（唯一合法签名）

```python
from autobiz.hardware.transports.mock import MockTransport

t = MockTransport(
    descriptor=dict,                    # validate 过的 descriptor
    address="mock://hak-v2/skill_name"  # 任意字符串，纯标识
)
```

**没有** `endpoint=` 或 `send=` 参数——传了会 TypeError。

## 命令协议

```python
# READ → 返回字符串（MockTransport._mock_read 返回 range midpoint）
t.write("READ temperature")
resp = t.read_line(timeout=5.0)   # "42.5"

# WRITE → 返回 "OK" 或 "ERR..."
t.write("WRITE set_temp -5")
resp = t.read_line()              # "OK"

# EXEC → 返回 "OK"
t.write("EXEC defrost")
resp = t.read_line()              # "OK"
```

## SkillRuntime 封装了一层

skill_runtime.py 的 `SkillRuntime` 自动处理了这些命令，**业务层不要直接调 MockTransport**：

```python
rt = engine().get("temperature_sensor")

# 推荐
rt.read("temperature")   # → 42.5 (自动类型转换)
rt.write("set_temp", -5) # → True
rt.execute("defrost")    # → True
rt.simulate(seconds=5)   # → [{t: 0.0, temperature: 42.5}, ...]
```

## 数值生成逻辑

`_mock_read(cap)` 返回 `range[midpoint]`，SkillRuntime 再叠加 2% 高斯噪声：

```python
mid = (lo + hi) / 2
noise = gauss(0, span * 0.02)
val = mid + noise
```

| range | mid | 典型输出 |
|---|---|---|
| [-40, 125] °C | 42.5 | 42.5 ± 3.3 |
| [0, 100] % | 50 | 50 ± 2 |
| [-16, 16] m/s² | 0 | 0 ± 0.64 |

整数 range 返回 int，浮点返回 float，自动 round 到 range 边界。

## 线程安全

SkillRuntime 内部有 `self._lock = threading.Lock()`，多线程并发读同一个 skill 不会撞 buffer。

## Pipeline/Trigger 里的 MockTransport

composer.py 的 Pipeline 和 Trigger 都是拿 SkillRuntime 当基础——Pipeline 每步调 `rt.read()/write()/execute()`，Trigger 调 `rt.read()` 然后和自动推导的阈值比较。都不需要自己管 MockTransport buffer。

## 调试技巧

```python
from autobiz.hardware.skill_runtime import SkillRuntime
rt = SkillRuntime("temperature_sensor")

# 看全部 capability
for c in rt.capabilities:
    print(c["name"], c["type"], c.get("range"), c.get("unit"))

# 强制读任意 capability
rt.read("temperature")          # 会按 SkillRuntime.parse_resp 返回数字
```
