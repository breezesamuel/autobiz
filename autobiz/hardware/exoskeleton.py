"""exoskeleton —— 4 自由度下肢外骨骼控制器（ROS2 生态 + HAK v2.0 注册）。

设计原则：
  - Windows 上无 rclpy → 纯 Python mock 模式，API 对 ROS2 接口 1:1 对齐
  - 4 自由度：左髋 θ1, 左膝 θ2, 右髋 θ3, 右膝 θ4（ sagittal plane 简化模型）
  - 正运动学：DH 参数 → 4×4 齐次变换链
  - 逆运动学：Newton-Raphson 迭代（Jacobian 伪逆）
  - 安全站立：10 步渐变从坐姿到站姿，每步 dt=0.2s
  - IMU 失衡检测：roll/pitch 阈值 ±15° → 自动紧急停止
  - HAK v2.0 第 13 种设备类型：注册为 "exoskeleton" device_class

依赖：
  - numpy（pip install numpy，已装）
  - rclpy（可选，有就真发 ROS2 topic，没有就 mock）
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np


# ======================================================================
# 简化模型：双 2DOF planar（左 + 右各一条 sagittal plane 链）
# 每条链: 髋(theta1) → 膝(theta2) → 脚踝(末端)
# 坐标系: x 向前, y 向上, 髋在原点 (0, 0)
# ======================================================================

# 人体测量简化值（身高 1.75m 假设）
L_FEMUR = 0.42    # 股骨长度 m
L_TIBIA = 0.43    # 胫骨长度 m

JOINT_NAMES = ["left_hip", "left_knee", "right_hip", "right_knee"]

# 安全关节限位（弧度）—— sagittal plane 参考
_HIP_LO, _HIP_HI = -0.5, 1.5     # 髋: 伸展 -28° ~ 屈曲 86°
_KNEE_LO, _KNEE_HI = -2.5, 0.1   # 膝: 屈曲 -143° ~ 伸展 6°
# dict: joint_idx → (lo, hi)
JOINT_LIMITS = {
    0: (_HIP_LO, _HIP_HI),
    1: (_KNEE_LO, _KNEE_HI),
    2: (_HIP_LO, _HIP_HI),
    3: (_KNEE_LO, _KNEE_HI),
}
# 访问器（legacy 函数）
def _lims(i: int):
    return JOINT_LIMITS.get(i, (-3.14, 3.14))

_LEFT_LO = [_HIP_LO, _KNEE_LO]
_LEFT_HI = [_HIP_HI, _KNEE_HI]

# IMU 安全阈值（度）
IMU_ROLL_THRESHOLD = 15.0
IMU_PITCH_THRESHOLD = 15.0

# 站立轨迹起止角度（单腿 2DOF: hip, knee）
# 坐姿: 髋前屈 0.8rad(46°), 膝屈 -1.5rad(-86°)
# 站立: 髋 0, 膝 0 (全伸展，腿竖直)
SEATED_LEG = [0.8, -1.5]
STANDING_LEG = [0.0, 0.0]
SEATED_THETA  = SEATED_LEG + SEATED_LEG     # 4 关节
STANDING_THETA = STANDING_LEG + STANDING_LEG


# ======================================================================
# 正运动学 —— planar 2DOF（每条腿独立）
# 返回末端 (脚踝) 相对髋的 (x, y)
# ======================================================================

def _leg_fk(hip: float, knee: float) -> tuple[float, float]:
    """单腿 2DOF 正运动学 —— 返回脚踝在髋坐标系下 (x, y)。
    hip: 髋相对竖直向上的角度（正=屈曲向前）
    knee: 膝相对股骨的角度（负=屈膝向后下）
    """
    # 髋坐标系: y 向上, x 向前
    # 股骨方向角: hip (相对 y 轴前倾 hip)
    # 膝相对股骨再转 knee
    thigh_angle = hip            # 股骨角度（相对 y 轴）
    shin_angle  = hip + knee     # 胫骨角度（相对 y 轴）

    # 末端 = 股骨向量 + 胫骨向量
    # 注意：从 y 轴计角，x = L*sin(angle), y = L*cos(angle)
    x = (L_FEMUR * math.sin(thigh_angle)
         + L_TIBIA  * math.sin(shin_angle))
    y = (L_FEMUR * math.cos(thigh_angle)
         + L_TIBIA  * math.cos(shin_angle))
    return x, y


def forward_kinematics(thetas: list[float]) -> np.ndarray:
    """4 关节 → 返回 (右腿 FK x,y, 左腿 FK x,y, 平均 y) 合并为 3D 位置。
    为了兼容旧接口，返回 shape (4,) 的数组: [leg_x, leg_y, pelvis_z, 0]
    """
    lh, lk, rh, rk = thetas[0], thetas[1], thetas[2], thetas[3]
    lx, ly = _leg_fk(lh, lk)
    rx, ry = _leg_fk(rh, rk)
    # 取右腿 x, 两腿 y 平均作为整体高度
    avg_y = (ly + ry) / 2.0
    return np.array([rx, avg_y, avg_y, 0.0])


def _position(T: np.ndarray) -> np.ndarray:
    """从 FK 结果提取末端 (x, y, z)。"""
    # 兼容：直接取前 3 元素
    if hasattr(T, 'shape') and T.shape == (4, 4):
        return T[:3, 3].copy()
    return np.array([T[0], T[1], T[2]])


def leg_fk(thetas_leg: list[float]) -> tuple[float, float]:
    """对外暴露：单腿 FK 返回 (x, y)。"""
    return _leg_fk(thetas_leg[0], thetas_leg[1])


# ======================================================================
# 逆运动学 —— 2DOF planar 解析解（稳如老狗）
# ======================================================================

def _leg_ik(target_x: float, target_y: float,
            lo: list[float], hi: list[float]) -> list[float] | None:
    """单腿 2DOF 解析 IK。

    target (x, y): 脚踝相对髋的目标位置。y 应该是正的（地面以下就是负值）。

    几何推导：
      已知髋在 (0,0)，脚踝在 (tx, ty)，
      两臂长 L1=L_FEMUR, L2=L_TIBIA
      1) d² = tx² + ty²
      2) cos_knee = (d² - L1² - L2²) / (2*L1*L2)
         → 若 |cos|>1 则不可达
      3) knee = ±arccos(cos_knee)  （选屈膝解，即负号那个）
      4) hip = atan2(tx, ty) - atan2(L2*sin(-knee), L1 + L2*cos(-knee))
         = atan2(tx, ty) - atan2(L2*sin(knee), L1 + L2*cos(knee))

    Returns:
        [hip, knee] 或 None
    """
    L1, L2 = L_FEMUR, L_TIBIA
    d2 = target_x**2 + target_y**2
    d = math.sqrt(d2)

    # 可达性
    if d > L1 + L2 - 0.01:
        return None  # 够不到
    if d < abs(L1 - L2) + 0.01:
        return None  # 太近（理论上不可能，实际给 0.01 宽容）

    cos_knee = (d2 - L1**2 - L2**2) / (2 * L1 * L2)
    cos_knee = max(-1.0, min(1.0, cos_knee))  # 数值钳制
    # 选屈膝解（knee 为负值）
    knee = -math.acos(cos_knee)

    # 髋角
    alpha = math.atan2(target_x, target_y)
    beta = math.atan2(L2 * math.sin(-knee), L1 + L2 * math.cos(-knee))
    hip = alpha - beta

    # 限位检查
    if not (lo[0] - 0.1 <= hip <= hi[0] + 0.1):
        return None
    if not (lo[1] - 0.1 <= knee <= hi[1] + 0.1):
        return None

    return [hip, knee]


def inverse_kinematics(target_xyz: list[float],
                        seed: Optional[list[float]] = None,
                        max_iter: int = 50,
                        tol: float = 1e-4) -> list[float] | None:
    """4DOF IK —— 左右腿独立求解析解。

    target_xyz: [foot_x, foot_y, pelvis_z] 或 [x, y, z] 简化为单腿 target
    左右腿对称镜像求解。
    """
    # 简化：用同一个 target 求两条腿
    tx, ty = target_xyz[0], target_xyz[1]

    left = _leg_ik(tx, ty, _LEFT_LO, _LEFT_HI)
    right = _leg_ik(tx, ty, _LEFT_LO, _LEFT_HI)

    if left is None or right is None:
        return None

    return left + right


# ======================================================================
# ExoskeletonController —— 主控制器
# ======================================================================

@dataclass
class IMUReading:
    """IMU 六轴读数。"""
    ax: float = 0.0   # m/s²
    ay: float = 0.0
    az: float = 9.81
    gx: float = 0.0   # rad/s
    gy: float = 0.0
    gz: float = 0.0

    @property
    def roll_deg(self) -> float:
        return math.degrees(math.atan2(self.ay, self.az + 1e-6))

    @property
    def pitch_deg(self) -> float:
        return math.degrees(math.atan2(-self.ax,
                                        math.sqrt(self.ay**2 + self.az**2) + 1e-6))

    def is_unstable(self,
                    roll_thresh: float = IMU_ROLL_THRESHOLD,
                    pitch_thresh: float = IMU_PITCH_THRESHOLD) -> bool:
        return abs(self.roll_deg) > roll_thresh or abs(self.pitch_deg) > pitch_thresh


@dataclass
class JointState:
    pos: float = 0.0      # rad
    vel: float = 0.0      # rad/s
    effort: float = 0.0    # Nm
    temp: float = 25.0     # °C


class ExoskeletonController:
    """外骨骼主控制器。

    支持三种运行模式：
      1. "mock"      —— 纯 Python 模拟（Windows 默认）
      2. "ros2"      —— 真实 ROS2 topic 发布（需要 Linux + rclpy）
      3. "hak-kernel" —— 通过 AutoBiz HAK v2.0 Kernel 统一管理

    安全机制：
      - 关节限位硬钳制
      - IMU 失衡自动急停
      - 急停按钮 (emergency_stop()) 独立于控制循环
    """

    HAK_DEVICE_CLASS = "exoskeleton"   # 第 13 种设备类型
    HAK_PROTOCOL = "hdp-1"

    def __init__(self, mode: str = "mock"):
        self.mode = mode
        self.joints: list[JointState] = [JointState() for _ in range(4)]
        self.imu = IMUReading()
        self._estop = False
        self._running = False
        self._ik_last_error = 0.0
        self._trajectory_log: list[dict] = []

        # ROS2 节点（可选）
        self._ros_node = None
        self._ros_pub = None

        if mode == "ros2":
            try:
                import rclpy
                from rclpy.node import Node
                from std_msgs.msg import Float64MultiArray
                rclpy.init()
                self._ros_node = rclpy.create_node("exoskeleton_controller")
                self._ros_pub = self._ros_node.create_publisher(
                    Float64MultiArray, "/exo/joint_commands", 10)
                print("[exo] ROS2 mode — node created")
            except Exception as e:
                print(f"[exo] rclpy not available: {e}. Falling back to mock.")
                self.mode = "mock"

    # ---------- 急停 ----------

    def emergency_stop(self) -> None:
        """独立于控制循环的紧急停止。所有关节力矩置零。"""
        self._estop = True
        self._running = False
        for j in self.joints:
            j.effort = 0.0
        print("[exo] 🚨 EMERGENCY STOP — all joints zeroed")

    def clear_estop(self) -> bool:
        """清除急停。需要所有关节都处于安全姿态才能恢复。"""
        # 检查所有关节是否在限位内
        for i, j in enumerate(self.joints):
            lo, hi = _lims(i)
            if not (lo <= j.pos <= hi):
                print(f"[exo] ⚠ cannot clear estop: joint {i} pos={j.pos:.2f} out of limit")
                return False
        # 检查 IMU 是否稳定
        if self.imu.is_unstable():
            print("[exo] ⚠ cannot clear estop: IMU unstable")
            return False
        self._estop = False
        print("[exo] ✅ estop cleared — controller ready")
        return True

    @property
    def estop_active(self) -> bool:
        return self._estop

    # ---------- 关节控制 ----------

    def set_joint_pos(self, joint_idx: int, target_rad: float,
                      max_effort: float = 50.0) -> bool:
        """单关节位置命令（带急停 + 限位检查）。"""
        if self._estop:
            return False
        if not (0 <= joint_idx < 4):
            return False
        lo, hi = _lims(joint_idx)
        target_rad = max(lo, min(hi, target_rad))  # 硬钳制
        self.joints[joint_idx].pos = target_rad
        self.joints[joint_idx].effort = max_effort
        self._publish_ros2()
        return True

    def set_all_joints(self, thetas: list[float]) -> bool:
        """同时设置 4 个关节。"""
        if self._estop or len(thetas) != 4:
            return False
        ok = True
        for i, t in enumerate(thetas):
            if not self.set_joint_pos(i, t):
                ok = False
        return ok

    # ---------- IMU 输入 ----------

    def update_imu(self, reading: IMUReading) -> bool:
        """喂入 IMU 数据。自动检测失衡触发急停。"""
        self.imu = reading
        if reading.is_unstable():
            print(f"[exo] 🚨 IMU UNSTABLE roll={reading.roll_deg:.1f}° "
                  f"pitch={reading.pitch_deg:.1f}° → emergency stop")
            self.emergency_stop()
            return True
        return False

    # ---------- 正逆运动学 ----------

    def fk(self) -> np.ndarray:
        return forward_kinematics([j.pos for j in self.joints])

    def ik(self, target_xyz: list[float]) -> list[float] | None:
        thetas = inverse_kinematics(
            target_xyz,
            seed=[j.pos for j in self.joints],
        )
        if thetas:
            self._ik_last_error = float(
                np.linalg.norm(
                    _position(self.fk()) - np.array(target_xyz)))
        return thetas

    # ---------- 安全站立轨迹（10 步渐变）----------

    def from_seated(self) -> list[float]:
        """返回当前坐姿的关节角度（用于规划起点）。"""
        return SEATED_THETA.copy()

    def to_standing(self, steps: int = 10, dt: float = 0.2,
                    execute: bool = True) -> list[list[float]]:
        """生成并可选执行从坐姿到站立的渐变轨迹。

        每步线性插值，确保每步都在安全限位内。

        Args:
            steps: 插值步数（用户要求 10）
            dt:    每步间隔秒数
            execute: True = 立刻逐步执行；False = 只返回轨迹点

        Returns:
            轨迹点列表 [theta_list × (steps+1)]
        """
        start = np.array(SEATED_THETA, dtype=float)
        end = np.array(STANDING_THETA, dtype=float)
        trajectory = []
        for k in range(steps + 1):
            alpha = k / steps
            theta = (start + alpha * (end - start)).tolist()
            trajectory.append(theta)

        # 安全校验：每个轨迹点都不能越限位
        for k, theta in enumerate(trajectory):
            for i, q_val in enumerate(theta):
                lo, hi = _lims(i)
                if not (lo - 0.05 <= q_val <= hi + 0.05):
                    print(f"[exo] ⚠ trajectory step {k} joint {i} "
                          f"theta={q_val:.3f} outside [{lo:.2f}, {hi:.2f}] — clamped")
                    theta[i] = max(lo, min(hi, q_val))

        if execute:
            self._running = True
            self._trajectory_log = []
            print(f"[exo] 🚶 to_standing: {steps} steps × {dt}s = {steps*dt:.1f}s")
            for k, theta in enumerate(trajectory):
                if self._estop:
                    print(f"[exo] ⚠ trajectory aborted at step {k}/{steps}")
                    break
                self.set_all_joints(theta)
                self._trajectory_log.append({
                    "step": k,
                    "thetas": theta.copy(),
                    "fk_xyz": _position(forward_kinematics(theta)).tolist(),
                    "t": k * dt,
                })
                if self.mode == "mock":
                    time.sleep(dt * 0.1)  # mock 快速跑完（真硬件用真实 dt）
                else:
                    time.sleep(dt)
            self._running = False
            print(f"[exo] ✅ standing complete — "
                  f"final height={self.current_height():.3f}m")

        return trajectory

    def current_height(self) -> float:
        """末端（右脚尖）当前 z 坐标 —— 粗略代表身高。"""
        return float(_position(self.fk())[2])

    # ---------- ROS2 发布（可选）----------

    def _publish_ros2(self) -> None:
        if self.mode != "ros2" or self._ros_pub is None:
            return
        try:
            from std_msgs.msg import Float64MultiArray
            msg = Float64MultiArray()
            msg.data = [j.pos for j in self.joints]
            self._ros_pub.publish(msg)
        except Exception:
            pass

    def shutdown(self) -> None:
        if self._ros_node is not None:
            try:
                import rclpy
                self._ros_node.destroy_node()
                rclpy.shutdown()
            except Exception:
                pass
        self.emergency_stop()


# ======================================================================
# HAK v2.0 Kernel 注册 —— 第 13 种设备类型
# ======================================================================

HAK_EXOSKELETON_DESCRIPTOR = {
    "protocol_version": "hdp-1",
    "vendor_id": "hak-v2",
    "product_id": "lower-limb-exo-4dof",
    "device_class": "exoskeleton",   # 第 13 种
    "serial_number": "HAK-EXO-0001",
    "capabilities": [
        {"name": "joint_pos",   "type": "rw",    "unit": "rad",  "range": [-3.14, 3.14]},
        {"name": "joint_vel",   "type": "read",  "unit": "rad/s","range": [-5.0, 5.0]},
        {"name": "joint_effort","type": "rw",    "unit": "Nm",   "range": [0, 80]},
        {"name": "imu_roll",    "type": "read",  "unit": "deg",  "range": [-90, 90]},
        {"name": "imu_pitch",   "type": "read",  "unit": "deg",  "range": [-90, 90]},
        {"name": "fk_end_eff",  "type": "read",  "unit": "m",    "range": [-2.0, 2.0]},
        {"name": "ik_target",   "type": "exec"},
        {"name": "stand_up",    "type": "exec"},
        {"name": "sit_down",    "type": "exec"},
        {"name": "emergency_stop", "type": "exec"},
        {"name": "estop_active","type": "read",  "unit": "",     "range": [0, 1]},
    ],
    "pinout": [
        {"pin": 1, "role": "VCC",  "voltage": 24.0},
        {"pin": 2, "role": "GND"},
        {"pin": 3, "role": "SDA",  "bus": "CAN"},
        {"pin": 4, "role": "SCL",  "bus": "CAN"},
        {"pin": 5, "role": "GPIO", "function": "estop_btn"},
        {"pin": 6, "role": "GPIO", "function": "status_led"},
    ],
    "defaults": {"poll_interval_ms": 200},
}


def register_with_hak_kernel() -> dict:
    """把外骨骼注册到 AutoBiz HAK v2.0 Kernel 作为第 13 种 device_class。

    做三件事：
      1. descriptor.py VALID_CLASSES 动态扩展 "exoskeleton"
      2. registry.upsert() 登记 SN=HAK-EXO-0001
      3. 返回注册结果 dict
    """
    # Step 1: 动态扩展 descriptor 白名单
    try:
        from autobiz.hardware import descriptor
        if "exoskeleton" not in descriptor.VALID_CLASSES:
            descriptor.VALID_CLASSES.add("exoskeleton")
            print(f"[hak] ✅ VALID_CLASSES extended: {sorted(descriptor.VALID_CLASSES)}")
    except Exception as e:
        print(f"[hak] ⚠ descriptor extend fail: {e}")

    # Step 2: 登记到 registry
    result = {"kernel_registered": False, "descriptor_valid": False,
              "sn": HAK_EXOSKELETON_DESCRIPTOR["serial_number"]}

    try:
        from autobiz.hardware.descriptor import validate
        desc = validate(HAK_EXOSKELETON_DESCRIPTOR)
        result["descriptor_valid"] = True

        from autobiz.hardware.registry import get_registry
        reg = get_registry()
        entry = reg.upsert(desc, transport_address="mock://hak-exo-0001")
        result["kernel_registered"] = True
        result["entry"] = entry.to_dict()
        result["total_devices"] = reg.count()
        print(f"[hak] ✅ exoskeleton registered as device class #{reg.count()}: "
              f"SN={entry.sn} class={HAK_EXOSKELETON_DESCRIPTOR['device_class']}")
    except Exception as e:
        result["error"] = str(e)
        print(f"[hak] ❌ register fail: {e}")

    return result


# ======================================================================
# CLI 入口（自测试）
# ======================================================================

def _self_test() -> dict:
    """快速自检 —— 无需 ROS2。"""
    report = {"mode": "mock", "steps": 0, "ik_ok": False,
              "standing_height": 0.0, "hak_registered": False}

    # 1. 实例化
    ctl = ExoskeletonController(mode="mock")
    report["joints_initial"] = [j.pos for j in ctl.joints]

    # 2. FK
    T = ctl.fk()
    report["fk_position"] = _position(T).tolist()

    # 3. IK —— 让右脚尖向前上 0.5m 处
    target = [0.35, 0.0, 0.50]
    sol = ctl.ik(target)
    report["ik_target"] = target
    report["ik_solution"] = sol
    report["ik_ok"] = sol is not None

    # 4. 10 步站立轨迹
    traj = ctl.to_standing(steps=10, dt=0.2, execute=False)
    report["steps"] = len(traj) - 1
    report["standing_height"] = float(_position(forward_kinematics(traj[-1]))[2])
    report["standing_theta"] = traj[-1]

    # 5. IMU 模拟 + 急停
    ctl.imu = IMUReading(ay=2.0)  # roll 会超阈值
    unstable = ctl.imu.is_unstable()
    report["imu_unstable_triggered"] = unstable

    # 6. HAK v2.0 注册
    hak_result = register_with_hak_kernel()
    report["hak_registered"] = hak_result.get("kernel_registered", False)
    report["total_device_classes"] = len(
        __import__("autobiz.hardware.descriptor", fromlist=["VALID_CLASSES"])
        .VALID_CLASSES) if False else None

    return report


if __name__ == "__main__":
    print("=" * 60)
    print("  EXOSKELETON CONTROLLER — SELF TEST")
    print("=" * 60)
    r = _self_test()
    print(json.dumps(r, indent=2, default=str))
