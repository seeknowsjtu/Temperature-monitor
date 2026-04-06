from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import List, Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

try:
    import serial
    from serial.tools import list_ports
except Exception:
    serial = None
    list_ports = None


DEFAULT_BASELINES = [
    74.0,
    30.0,
    78.0,
    84.85,
    86.05,
    68.3,
    112.15,
    95.0,
    79.0,
    84.2,
    62.0,
    65.0,
    45.0,
    55.0,
    72.0,
    81.0,
]


def hex_to_bytes(hex_text: str) -> bytes:
    normalized = hex_text.replace(" ", "").strip()
    if len(normalized) % 2 != 0:
        raise ValueError("十六进制命令长度必须为偶数。")
    return bytes.fromhex(normalized)


@dataclass
class SerialSettings:
    port: str = "COM3"
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout: float = 0.25
    write_timeout: float = 0.25
    fixed_request_hex: str = "0103000000104406"
    poll_interval_ms: int = 500
    value_scale: float = 10.0
    value_limit: float = 500.0
    signed_short: bool = False


class MIKLabVIEWCompatibleClient:
    """
    尽量贴近当前 LabVIEW 程序的设备层行为：
    1. 固定请求帧默认使用 01 03 00 00 00 10 44 06
    2. 每次读取完整返回帧
    3. 16 路数据按 2 字节顺序拆分
    4. 默认保留 /10 缩放和 >500 归零保护
    """

    CHANNEL_COUNT = 16
    RESPONSE_LEN = 37  # 1 addr + 1 func + 1 byte_count + 32 data + 2 crc

    def __init__(self, settings: SerialSettings) -> None:
        self.settings = settings
        self.ser = None

    @staticmethod
    def available_ports() -> List[str]:
        if list_ports is None:
            return []
        return [info.device for info in list_ports.comports()]

    def open(self) -> None:
        if serial is None:
            raise RuntimeError("未检测到 pyserial，请先安装 pyserial。")

        self.close()
        self.ser = serial.Serial(
            port=self.settings.port,
            baudrate=self.settings.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.settings.timeout,
            write_timeout=self.settings.write_timeout,
        )
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None

    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def recv_exact(self, size: int) -> bytes:
        if not self.is_open:
            raise RuntimeError("串口尚未打开。")
        assert self.ser is not None

        chunks: List[bytes] = []
        received = 0
        start = time.perf_counter()
        while received < size:
            part = self.ser.read(size - received)
            if part:
                chunks.append(part)
                received += len(part)
                continue
            if time.perf_counter() - start > max(1.0, 4 * self.settings.timeout):
                break
        return b"".join(chunks)

    def query_frame(self) -> bytes:
        if not self.is_open:
            raise RuntimeError("串口尚未打开。")

        request = hex_to_bytes(self.settings.fixed_request_hex)
        assert self.ser is not None
        self.ser.reset_input_buffer()
        self.ser.write(request)
        self.ser.flush()

        frame = self.recv_exact(self.RESPONSE_LEN)
        if len(frame) != self.RESPONSE_LEN:
            raise TimeoutError(
                f"读取返回帧长度不对: got {len(frame)}, expected {self.RESPONSE_LEN}"
            )
        return frame

    def parse_values(self, frame: bytes) -> List[float]:
        if len(frame) < self.RESPONSE_LEN:
            raise ValueError("响应帧过短。")
        if frame[1] != 0x03:
            raise ValueError(f"功能码不对: 0x{frame[1]:02X}")
        if frame[2] != 32:
            raise ValueError(f"字节数不对: {frame[2]}")

        values: List[float] = []
        for i in range(self.CHANNEL_COUNT):
            hi = frame[3 + 2 * i]
            lo = frame[4 + 2 * i]
            raw = (hi << 8) | lo
            if self.settings.signed_short and raw >= 0x8000:
                raw -= 0x10000
            value = raw / self.settings.value_scale
            # 先尽量贴近当前 LabVIEW 图里的比较方式：
            # 只对“大于阈值”的值做归零保护，不做 abs(value) 判断。
            if value > self.settings.value_limit:
                value = 0.0
            values.append(value)
        return values

    def read_values(self) -> tuple[List[float], bytes]:
        frame = self.query_frame()
        return self.parse_values(frame), frame


class SerialWorker(QObject):
    values_ready = Signal(list, bytes)
    error_occurred = Signal(str)
    connection_changed = Signal(bool, str)

    def __init__(self, settings: SerialSettings) -> None:
        super().__init__()
        self.settings = settings
        self.client = MIKLabVIEWCompatibleClient(settings)
        self.timer: Optional[QTimer] = None
        self.mock_mode = True
        self.running = False
        self.t0 = time.perf_counter()

    @Slot(bool)
    def set_mock_mode(self, enabled: bool) -> None:
        self.mock_mode = enabled

    @Slot()
    def open_port(self) -> None:
        if self.mock_mode:
            self.connection_changed.emit(True, "Mock connected")
            return
        try:
            self.client.open()
            self.connection_changed.emit(True, f"Connected: {self.settings.port}")
        except Exception as exc:
            self.connection_changed.emit(False, f"Connect failed: {exc}")

    @Slot()
    def close_port(self) -> None:
        self.stop_polling()
        try:
            self.client.close()
        finally:
            self.connection_changed.emit(False, "Disconnected")

    @Slot()
    def start_polling(self) -> None:
        if self.running:
            return
        self.running = True
        if self.timer is None:
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.poll_once)
        self.timer.setInterval(self.settings.poll_interval_ms)
        self.timer.start()
        self.t0 = time.perf_counter()

    @Slot()
    def stop_polling(self) -> None:
        self.running = False
        if self.timer is not None:
            self.timer.stop()

    @Slot()
    def poll_once(self) -> None:
        try:
            if self.mock_mode:
                values = self._generate_mock_values()
                frame = b""
            else:
                values, frame = self.client.read_values()
            self.values_ready.emit(values, frame)
        except Exception as exc:
            self.error_occurred.emit(str(exc))

    def _generate_mock_values(self) -> List[float]:
        elapsed = time.perf_counter() - self.t0
        out: List[float] = []
        for idx, baseline in enumerate(DEFAULT_BASELINES):
            wave = 0.12 * math.sin(elapsed * 0.4 + idx * 0.5)
            noise = random.uniform(-0.03, 0.03)
            out.append(baseline + wave + noise)
        return out
