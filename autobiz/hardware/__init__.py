"""autobiz.hardware —— 自主硬件管理内核（Self-governing Hardware Kernel）。

对外主要导出：

  from autobiz.hardware import Kernel
  from autobiz.hardware import discovery
  from autobiz.hardware import make_mock_descriptor
  from autobiz.hardware.transports import MockTransport, SerialTransport, TCPTransport
"""
from .kernel import Kernel, Device
from .descriptor import (DescriptorError, validate, make_mock_descriptor,
                          capability_names)
from .handshake import Handshake, HandshakeError
from .registry import Registry, DeviceEntry, get_registry
from .semantic import (PLATFORM_VOCABULARY, negotiate, translate, add_vocab)
from .sandbox import SandboxError, validate_command, validate_capability
from .learning import (LearningState, get_state, adaptive_poll_interval)
from .transports import Transport, MockTransport, SerialTransport, TCPTransport

__all__ = [
    "Kernel", "Device",
    "DescriptorError", "validate", "make_mock_descriptor", "capability_names",
    "Handshake", "HandshakeError",
    "Registry", "DeviceEntry", "get_registry",
    "PLATFORM_VOCABULARY", "negotiate", "translate", "add_vocab",
    "SandboxError", "validate_command", "validate_capability",
    "LearningState", "get_state", "adaptive_poll_interval",
    "Transport", "MockTransport", "SerialTransport", "TCPTransport",
]
