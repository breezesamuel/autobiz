"""autobiz.hardware - 自主硬件管理内核 (Self-governing Hardware Kernel).

核心流程: 发现 -> 握手 -> 注册 -> 读写控制 -> 健康管理

  from autobiz.hardware import Kernel
  from autobiz.hardware import make_mock_descriptor
  from autobiz.hardware.transports import MockTransport, SerialTransport, TCPTransport
  from autobiz.hardware.skill_runtime import SkillRuntime, SkillEngine, engine
  from autobiz.hardware.skill_runtime import DataBus, get_bus, reset_bus
"""
from .kernel import Kernel, Device, get_kernel
from .descriptor import (DescriptorError, validate, make_mock_descriptor,
                          capability_names)
from .handshake import Handshake, HandshakeError
from .registry import Registry, DeviceEntry, get_registry
from .semantic import (PLATFORM_VOCABULARY, negotiate, translate, add_vocab)
from .sandbox import SandboxError, validate_command, validate_capability
from .learning import (LearningState, get_state, adaptive_poll_interval)
from .transports import Transport, MockTransport, SerialTransport, TCPTransport

# 120 skills runtime - descriptor 协议的实例化 (增强但保留)
from .skill_runtime import (SkillRuntime, SkillEngine, engine,
                            DataBus, get_bus, reset_bus)
from .skill_factory import SkillFactory

__all__ = [
    # 核心 8 件事
    "Kernel", "Device", "get_kernel",
    "DescriptorError", "validate", "make_mock_descriptor", "capability_names",
    "Handshake", "HandshakeError",
    "Registry", "DeviceEntry", "get_registry",
    "PLATFORM_VOCABULARY", "negotiate", "translate", "add_vocab",
    "SandboxError", "validate_command", "validate_capability",
    "LearningState", "get_state", "adaptive_poll_interval",
    "Transport", "MockTransport", "SerialTransport", "TCPTransport",
    # 120 skills runtime 增强
    "SkillRuntime", "SkillEngine", "engine",
    "DataBus", "get_bus", "reset_bus",
    "SkillFactory",
]
