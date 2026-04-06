from __future__ import annotations

import csv
import time
from collections import deque
from pathlib import Path
from typing import List

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from device import MIKLabVIEWCompatibleClient, SerialSettings, SerialWorker, hex_to_bytes


CHANNEL_NAMES = [
    "MC分子泵",
    "MC离子泵",
    "MC腔体上",
    "能量分析器后",
    "MC腔体下",
    "旋转密封下",
    "MC中",
    "LENS",
    "NIR窗口",
    "能量分析器前",
    "MC顶部法兰",
    "MC二级波纹管",
    "Ion pump门阀",
    "旋转密封上",
    "MC波纹管",
    "MIR窗口",
]


class MainWindow(QMainWindow):
    CHANNEL_COUNT = 16

    request_open_port = Signal()
    request_close_port = Signal()
    request_start_polling = Signal()
    request_stop_polling = Signal()
    request_set_mock_mode = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Heating Monitor - Python Rebuild")
        self.resize(1800, 980)

        self.settings = SerialSettings()
        self.connected = False
        self.running = False
        self.max_points = 10000
        self.time_buffer = deque(maxlen=self.max_points)
        self.data_buffers = [deque(maxlen=self.max_points) for _ in range(self.CHANNEL_COUNT)]
        self.current_values = [0.0 for _ in range(self.CHANNEL_COUNT)]
        self.value_edits: List[QLineEdit] = []
        self.value_title_labels: List[QLabel] = []
        self.plot_widgets: List[pg.PlotWidget] = []
        self.curves = []
        self.session_t0 = time.perf_counter()
        self.last_frame_hex = ""
        self.auto_save_enabled = True

        self.worker_thread = QThread(self)
        self.worker = SerialWorker(self.settings)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.start()

        self.request_open_port.connect(self.worker.open_port)
        self.request_close_port.connect(self.worker.close_port)
        self.request_start_polling.connect(self.worker.start_polling)
        self.request_stop_polling.connect(self.worker.stop_polling)
        self.request_set_mock_mode.connect(self.worker.set_mock_mode)

        self.worker.values_ready.connect(self.on_values_ready)
        self.worker.error_occurred.connect(self.on_worker_error)
        self.worker.connection_changed.connect(self.on_connection_changed)

        self._build_ui()
        self._refresh_ports()
        self._sync_selected_name()
        self._log("程序启动。")
        self._log("当前结构已拆成三个文件：main / ui / device。")
        self._log("默认兼容 LabVIEW 行为：01 03 00 00 00 10 44 06，500 ms 轮询。")

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        top_layout = QHBoxLayout()
        outer.addLayout(top_layout, 0)
        top_layout.addWidget(self._build_summary_panel(), 4)
        top_layout.addWidget(self._build_control_panel(), 1)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_plot_tab(0, 8), "channel 1-8")
        self.tabs.addTab(self._build_plot_tab(8, 16), "channel 9-16")

        outer.addWidget(self._build_log_panel(), 0)
        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Idle")

    def _build_summary_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        layout = QGridLayout(frame)

        for idx, name in enumerate(CHANNEL_NAMES):
            row = idx // 8
            col = idx % 8
            box = QVBoxLayout()
            label = QLabel(name)
            edit = QLineEdit("0")
            edit.setReadOnly(True)
            edit.setAlignment(Qt.AlignRight)
            edit.setMinimumWidth(110)
            self.value_title_labels.append(label)
            self.value_edits.append(edit)
            box.addWidget(label)
            box.addWidget(edit)

            holder = QWidget()
            holder.setLayout(box)
            layout.addWidget(holder, row, col)

        return frame

    def _build_control_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        layout = QGridLayout(frame)

        self.port_combo = QComboBox()
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.baud_combo.setCurrentText(str(self.settings.baudrate))

        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setEnabled(False)
        self.refresh_btn = QPushButton("Refresh Ports")

        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)

        self.channel_select_spin = QSpinBox()
        self.channel_select_spin.setRange(1, 16)
        self.channel_select_spin.setValue(1)

        self.datapoints_spin = QSpinBox()
        self.datapoints_spin.setRange(100, 200000)
        self.datapoints_spin.setValue(10000)

        self.name_edit = QLineEdit()
        self.set_btn = QPushButton("set")

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Real Serial", "Mock Data"])
        self.mode_combo.setCurrentText("Mock Data")

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.001, 10000.0)
        self.scale_spin.setValue(self.settings.value_scale)
        self.scale_spin.setDecimals(3)

        self.limit_spin = QDoubleSpinBox()
        self.limit_spin.setRange(1.0, 100000.0)
        self.limit_spin.setValue(self.settings.value_limit)
        self.limit_spin.setDecimals(1)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(50, 10000)
        self.interval_spin.setValue(self.settings.poll_interval_ms)
        self.interval_spin.setSuffix(" ms")

        self.path_edit = QLineEdit(r"C:\Users\DELL\Desktop\heating data")
        self.request_edit = QLineEdit("0103 0000 0010 4406")
        self.last_frame_edit = QLineEdit()
        self.last_frame_edit.setReadOnly(True)

        layout.addWidget(QLabel("Port"), 0, 0)
        layout.addWidget(self.port_combo, 0, 1)
        layout.addWidget(QLabel("Baud"), 1, 0)
        layout.addWidget(self.baud_combo, 1, 1)
        layout.addWidget(self.refresh_btn, 2, 0, 1, 2)
        layout.addWidget(self.connect_btn, 3, 0)
        layout.addWidget(self.disconnect_btn, 3, 1)

        layout.addWidget(QLabel("channel select"), 4, 0)
        layout.addWidget(self.channel_select_spin, 4, 1)
        layout.addWidget(QLabel("datapoints"), 5, 0)
        layout.addWidget(self.datapoints_spin, 5, 1)
        layout.addWidget(QLabel("name"), 6, 0)
        layout.addWidget(self.name_edit, 6, 1)
        layout.addWidget(self.set_btn, 7, 0, 1, 2)

        layout.addWidget(QLabel("mode"), 8, 0)
        layout.addWidget(self.mode_combo, 8, 1)
        layout.addWidget(QLabel("scale"), 9, 0)
        layout.addWidget(self.scale_spin, 9, 1)
        layout.addWidget(QLabel("limit"), 10, 0)
        layout.addWidget(self.limit_spin, 10, 1)
        layout.addWidget(QLabel("interval"), 11, 0)
        layout.addWidget(self.interval_spin, 11, 1)

        layout.addWidget(QLabel("save path"), 12, 0)
        layout.addWidget(self.path_edit, 12, 1)
        layout.addWidget(QLabel("read cmd"), 13, 0)
        layout.addWidget(self.request_edit, 13, 1)
        layout.addWidget(QLabel("last frame"), 14, 0)
        layout.addWidget(self.last_frame_edit, 14, 1)

        self.clear_btn = QPushButton("clear")
        self.quit_btn = QPushButton("Quit")
        layout.addWidget(self.start_btn, 15, 0)
        layout.addWidget(self.stop_btn, 15, 1)
        layout.addWidget(self.clear_btn, 16, 0)
        layout.addWidget(self.quit_btn, 16, 1)

        self._wire_control_signals()
        return frame

    def _build_plot_tab(self, start: int, end: int) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)

        for local_idx, ch_idx in enumerate(range(start, end)):
            plot = pg.PlotWidget()
            plot.setBackground("w")
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.setTitle(CHANNEL_NAMES[ch_idx])
            plot.setLabel("left", "temp")
            plot.setLabel("bottom", "time")
            curve = plot.plot([], [], pen=pg.mkPen(width=2))
            self.plot_widgets.append(plot)
            self.curves.append(curve)

            row = local_idx // 4
            col = local_idx % 4
            layout.addWidget(plot, row, col)

        return page

    def _build_log_panel(self) -> QWidget:
        box = QGroupBox("Log")
        layout = QVBoxLayout(box)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(160)
        layout.addWidget(self.log_edit)
        return box

    def _wire_control_signals(self) -> None:
        self.refresh_btn.clicked.connect(self._refresh_ports)
        self.connect_btn.clicked.connect(self.connect_serial)
        self.disconnect_btn.clicked.connect(self.disconnect_serial)
        self.start_btn.clicked.connect(self.start_polling)
        self.stop_btn.clicked.connect(self.stop_polling)
        self.clear_btn.clicked.connect(self.clear_history)
        self.quit_btn.clicked.connect(self.close)
        self.set_btn.clicked.connect(self.rename_selected_channel)
        self.channel_select_spin.valueChanged.connect(self._sync_selected_name)
        self.datapoints_spin.valueChanged.connect(self._apply_new_datapoints)
        self.scale_spin.valueChanged.connect(self._update_scale)
        self.limit_spin.valueChanged.connect(self._update_limit)
        self.interval_spin.valueChanged.connect(self._update_interval)
        self.baud_combo.currentTextChanged.connect(self._update_baud)
        self.port_combo.currentTextChanged.connect(self._update_port)
        self.mode_combo.currentTextChanged.connect(self._update_mode)
        self.request_edit.editingFinished.connect(self._update_request)

    def _sync_selected_name(self) -> None:
        idx = self.channel_select_spin.value() - 1
        self.name_edit.setText(CHANNEL_NAMES[idx])

    def rename_selected_channel(self) -> None:
        idx = self.channel_select_spin.value() - 1
        new_name = self.name_edit.text().strip()
        if not new_name:
            return
        CHANNEL_NAMES[idx] = new_name
        self.value_title_labels[idx].setText(new_name)
        self.plot_widgets[idx].setTitle(new_name)
        self._log(f"已修改通道 {idx + 1} 名称为: {new_name}")

    def _apply_new_datapoints(self, value: int) -> None:
        self.max_points = value
        self.time_buffer = deque(self.time_buffer, maxlen=value)
        self.data_buffers = [deque(buf, maxlen=value) for buf in self.data_buffers]
        self._log(f"datapoints 更新为 {value}")

    def _update_scale(self, value: float) -> None:
        self.settings.value_scale = value
        self._log(f"scale 更新为 {value}")

    def _update_limit(self, value: float) -> None:
        self.settings.value_limit = value
        self._log(f"limit 更新为 {value}")

    def _update_interval(self, value: int) -> None:
        self.settings.poll_interval_ms = value
        self._log(f"poll interval 更新为 {value} ms")

    def _update_baud(self, text: str) -> None:
        self.settings.baudrate = int(text)

    def _update_port(self, text: str) -> None:
        self.settings.port = text

    def _update_mode(self, text: str) -> None:
        self.request_set_mock_mode.emit(text == "Mock Data")
        self._log(f"mode 切换为 {text}")

    def _update_request(self) -> None:
        try:
            hex_to_bytes(self.request_edit.text())
            self.settings.fixed_request_hex = self.request_edit.text().replace(" ", "")
            self._log(f"读取命令更新为 {self.request_edit.text()}")
        except Exception as exc:
            QMessageBox.warning(self, "Invalid Command", str(exc))

    def _refresh_ports(self) -> None:
        self.port_combo.clear()
        ports = MIKLabVIEWCompatibleClient.available_ports()
        if not ports:
            ports = ["COM3"]
        self.port_combo.addItems(ports)
        if self.settings.port in ports:
            self.port_combo.setCurrentText(self.settings.port)
        elif "COM3" in ports:
            self.port_combo.setCurrentText("COM3")
        self._log(f"串口列表刷新: {', '.join(ports)}")

    def connect_serial(self) -> None:
        self.settings.port = self.port_combo.currentText()
        self.settings.baudrate = int(self.baud_combo.currentText())
        self.settings.fixed_request_hex = self.request_edit.text().replace(" ", "")
        self.request_set_mock_mode.emit(self.mode_combo.currentText() == "Mock Data")
        self.request_open_port.emit()

    def disconnect_serial(self) -> None:
        self.stop_polling()
        self.request_close_port.emit()

    def start_polling(self) -> None:
        if not self.connected:
            QMessageBox.warning(self, "Warning", "请先连接设备。")
            return
        if self.running:
            return
        self.running = True
        self.session_t0 = time.perf_counter()
        self.request_start_polling.emit()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.statusBar().showMessage("Polling...")
        self._log("开始轮询。")

    def stop_polling(self) -> None:
        if not self.running:
            return
        self.running = False
        self.request_stop_polling.emit()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage("Stopped")
        self._log("停止轮询。")

    def clear_history(self) -> None:
        self.time_buffer.clear()
        self.data_buffers = [deque(maxlen=self.max_points) for _ in range(self.CHANNEL_COUNT)]
        for curve in self.curves:
            curve.setData([], [])
        for edit in self.value_edits:
            edit.setText("0")
        self._log("历史曲线已清空。")

    @Slot(list, bytes)
    def on_values_ready(self, values: List[float], frame: bytes) -> None:
        if len(values) != self.CHANNEL_COUNT:
            self._log(f"通道数异常: got {len(values)}")
            return

        t = time.perf_counter() - self.session_t0
        self.time_buffer.append(t)
        for idx, value in enumerate(values):
            self.current_values[idx] = value
            self.data_buffers[idx].append(value)
            self.value_edits[idx].setText(f"{value:.3f}")
            self.curves[idx].setData(list(self.time_buffer), list(self.data_buffers[idx]))

        if frame:
            self.last_frame_hex = frame.hex(" ").upper()
            self.last_frame_edit.setText(self.last_frame_hex)
        if self.auto_save_enabled:
            self._append_row(values)

    @Slot(str)
    def on_worker_error(self, text: str) -> None:
        self._log(f"轮询失败: {text}")
        if self.mode_combo.currentText() != "Mock Data":
            self.stop_polling()
            QMessageBox.warning(self, "Polling Error", text)

    @Slot(bool, str)
    def on_connection_changed(self, ok: bool, message: str) -> None:
        self.connected = ok
        self.connect_btn.setEnabled(not ok)
        self.disconnect_btn.setEnabled(ok)
        self.statusBar().showMessage(message)
        self._log(message)

    def _append_row(self, values: List[float]) -> None:
        path = Path(self.path_edit.text().strip() or ".")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            return

        file_path = path / f"{time.strftime('%Y%m%d')}.csv"
        new_file = not file_path.exists()
        with file_path.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["timestamp", *CHANNEL_NAMES])
            writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), *[f"{v:.3f}" for v in values]])

    def _log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_edit.append(f"[{stamp}] {text}")

    def closeEvent(self, event) -> None:
        try:
            self.request_stop_polling.emit()
            self.request_close_port.emit()
            self.worker_thread.quit()
            self.worker_thread.wait(1500)
        finally:
            event.accept()
