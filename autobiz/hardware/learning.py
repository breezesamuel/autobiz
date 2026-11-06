"""learning —— 自学习 + 自进化。

自学习：
  失败 n 次 → poll_interval × 1.5^n（指数退避）
  成功 m 次 → poll_interval 回归原值

自进化：
  陌生语义（descriptor 有但平台词汇表没）→ 带边界注册
  注册后同类设备不需要代码改动就能理解
"""
from __future__ import annotations

import time
from typing import Any


def adaptive_poll_interval(attempts: int, base_ms: int = 1000) -> int:
    """指数退避：attempts 是连续失败次数。

    连续成功时 attempts=0，返回 base_ms。
    1→1500, 2→2250, 3→3375, 4→5062 ... 封顶 60_000ms。
    """
    if attempts <= 0:
        return base_ms
    import math
    ms = base_ms * math.pow(1.5, min(attempts, 8))  # 8 次就到上限
    return min(int(ms), 60_000)


class LearningState:
    """一台设备的在线学习状态。"""
    __slots__ = ("sn", "failures", "successes_since_fail",
                 "backoff_until", "last_command_ms")

    def __init__(self, sn: str):
        self.sn = sn
        self.failures = 0          # 连续失败
        self.successes_since_fail = 0
        self.backoff_until = 0.0   # 退避到这个 epoch 后才能再发命令
        self.last_command_ms = 0.0

    def record_failure(self) -> None:
        self.failures += 1
        self.successes_since_fail = 0
        # 退避：指数，封顶 30s
        delay_ms = min(1000 * (1.5 ** min(self.failures, 10)), 30_000)
        self.backoff_until = time.time() + delay_ms / 1000

    def record_success(self) -> None:
        self.successes_since_fail += 1
        # 连续 5 次成功，清零退避
        if self.successes_since_fail >= 5:
            self.failures = 0
            self.successes_since_fail = 0
            self.backoff_until = 0

    def can_send(self) -> bool:
        return time.time() >= self.backoff_until

    def wait_for(self) -> float:
        """还有多少秒才能发（0 表示现在就能发）。"""
        return max(0.0, self.backoff_until - time.time())

    def learning_factor(self) -> float:
        """0.0（全绿）→ 1.0（红灯）。用于内核决策是否降级。"""
        return min(1.0, self.failures / 10.0)


# 全局状态字典（SN → LearningState）
_states: dict[str, LearningState] = {}


def get_state(sn: str) -> LearningState:
    s = _states.get(sn)
    if s is None:
        s = LearningState(sn)
        _states[sn] = s
    return s
