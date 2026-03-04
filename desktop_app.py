import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
from PySide6.QtCore import QEvent, QThread, Qt, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QFont, QIntValidator, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSlider,
    QLineEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from controller_actions import ControllerActions
from constants import DEFAULT_CAMERA_IP, DEFAULT_GIMBAL_PORT, DEFAULT_RTSP_URL
from viewpro_gimbal import ViewProGimbal

CAMERA_RTSP_URL = DEFAULT_RTSP_URL
GIMBAL_TCP_IP = DEFAULT_CAMERA_IP
GIMBAL_TCP_PORT = DEFAULT_GIMBAL_PORT


@dataclass
class AppState:
    mode: str = "auto"
    view: str = "thermal"
    movement: str = "idle"
    speed: int = 100


class RtspReader(QThread):
    frame_ready = Signal(object)
    stream_status = Signal(bool, str)

    def __init__(self, rtsp_url: str):
        super().__init__()
        self.rtsp_url = rtsp_url
        self._running = True
        self._cap_lock = threading.Lock()
        self._cap = None

    def stop(self):
        self._running = False
        self.requestInterruption()
        self._release_capture()
        if not self.wait(3000):
            # Last-resort guard to avoid "QThread destroyed while thread is still running".
            self.terminate()
            self.wait(1000)

    def _set_capture(self, cap):
        with self._cap_lock:
            self._cap = cap

    def _release_capture(self):
        with self._cap_lock:
            cap = self._cap
            self._cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _open_capture(self):
        attempts = [
            ("tcp", "rtsp_transport;tcp"),
            ("udp", "rtsp_transport;udp"),
            ("auto", None),
        ]

        for label, ffmpeg_opts in attempts:
            if ffmpeg_opts:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = ffmpeg_opts
            else:
                os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)

            cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(self.rtsp_url)

            if cap.isOpened():
                return cap, label

            cap.release()

        return None, None

    def run(self):
        reconnect_delay = 1.0

        while self._running and not self.isInterruptionRequested():
            cap = None
            try:
                cap, transport = self._open_capture()
                if cap is None:
                    self.stream_status.emit(False, "Cannot open RTSP stream. Retrying...")
                    time.sleep(reconnect_delay)
                    continue

                self._set_capture(cap)
                self.stream_status.emit(True, f"Connected ({transport.upper()})")
                fail_count = 0

                while self._running and not self.isInterruptionRequested():
                    ok, frame = cap.read()
                    if not ok:
                        fail_count += 1
                        if fail_count > 30:
                            self.stream_status.emit(False, "Stream lost. Reconnecting...")
                            break
                        continue

                    fail_count = 0
                    self.frame_ready.emit(frame)

            except Exception as ex:
                self.stream_status.emit(False, f"Stream error: {ex}. Reconnecting...")
            finally:
                self._release_capture()

            if self._running and not self.isInterruptionRequested():
                time.sleep(reconnect_delay)


class SegmentRecorder:
    def __init__(self, output_dir: Path, segment_seconds: int = 300, fps: float = 20.0):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.segment_seconds = segment_seconds
        self.fps = fps
        self._is_recording = False
        self._writer = None
        self._segment_started_at = 0.0
        self._segment_mode = ""
        self._segment_index = 0
        self._frame_size = None
        self._current_path = None

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def current_path(self):
        return self._current_path

    def start(self):
        self._is_recording = True
        self._segment_index = 0
        self._segment_started_at = 0.0
        self._segment_mode = ""
        self._frame_size = None
        self._current_path = None

    def stop(self):
        self._is_recording = False
        self._release_writer()
        self._current_path = None

    def set_segment_seconds(self, seconds: int):
        self.segment_seconds = max(30, int(seconds))
        if self._is_recording and self._writer is not None:
            # Force segment rollover on next frame so new duration applies immediately.
            self._segment_started_at = 0.0

    def write_frame(self, frame, view_mode: str):
        if not self._is_recording:
            return None

        h, w = frame.shape[:2]
        frame_size = (w, h)
        now = time.time()
        segment_expired = (now - self._segment_started_at) >= self.segment_seconds
        view_changed = view_mode != self._segment_mode
        size_changed = self._frame_size != frame_size

        if self._writer is None or segment_expired or view_changed or size_changed:
            self._start_new_segment(frame_size, view_mode, now)

        if self._writer is not None:
            self._writer.write(frame)

        return self._current_path

    def _start_new_segment(self, frame_size, view_mode: str, started_at: float):
        self._release_writer()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_mode = (view_mode or "unknown").lower()
        file_name = f"{stamp}_{safe_mode}_{self._segment_index:03d}.mp4"
        path = self.output_dir / file_name
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, self.fps, frame_size)
        if not writer.isOpened():
            self._current_path = None
            return

        self._writer = writer
        self._segment_started_at = started_at
        self._segment_mode = view_mode
        self._frame_size = frame_size
        self._current_path = path
        self._segment_index += 1

    def _release_writer(self):
        if self._writer is not None:
            self._writer.release()
            self._writer = None


class ControllerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Thermal Camera Controller")
        self.resize(1280, 760)

        self.state = AppState()
        self.default_rtsp_url = CAMERA_RTSP_URL
        self.current_rtsp_url = CAMERA_RTSP_URL
        self.default_gimbal_ip = GIMBAL_TCP_IP
        self.default_gimbal_port = GIMBAL_TCP_PORT
        self.current_gimbal_ip = GIMBAL_TCP_IP
        self.current_gimbal_port = GIMBAL_TCP_PORT
        self.reader = None
        self.panel_expanded_width = 330
        self.panel_visible = True
        self.gimbal = ViewProGimbal(host=self.current_gimbal_ip, port=self.current_gimbal_port)
        self.recorder = SegmentRecorder(Path(__file__).resolve().parent / "recordings", segment_seconds=300)
        self.segment_minutes = 5
        self.latest_frame = None
        self._active_keyboard_moves = set()
        self._is_preview_fullscreen = False
        self._panel_visible_before_fullscreen = True
        self._normal_shell_margins = None
        self._normal_shell_spacing = None
        self._normal_stream_margins = None
        self._normal_stream_spacing = None

        self._build_ui()
        self.actions = ControllerActions(self)
        self.actions.bind_events()
        self._apply_theme()
        self.actions.render_state()
        self._connect_gimbal()
        self._restart_stream_reader()
        self.setFocusPolicy(Qt.StrongFocus)
        self.installEventFilter(self)
        QApplication.instance().installEventFilter(self)

    def _connect_gimbal(self):
        try:
            self.gimbal.connect()
            if hasattr(self, "settings_status_label"):
                self.settings_status_label.setText(
                    f"Gimbal connected: {self.current_gimbal_ip}:{self.current_gimbal_port}"
                )
        except Exception as ex:
            if hasattr(self, "settings_status_label"):
                self.settings_status_label.setText(f"Gimbal connection failed: {ex}")

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)

        shell = QHBoxLayout(root)
        shell.setContentsMargins(14, 14, 14, 14)
        shell.setSpacing(14)
        self.shell_layout = shell

        stream_panel = QFrame()
        stream_panel.setObjectName("streamPanel")
        stream_layout = QVBoxLayout(stream_panel)
        stream_layout.setContentsMargins(12, 12, 12, 12)
        stream_layout.setSpacing(10)
        self.stream_layout = stream_layout

        head = QHBoxLayout()
        title = QLabel("THERMAL CAMERA")
        title.setObjectName("title")
        self.fullscreen_btn = QPushButton("Full Screen")
        self.fullscreen_btn.setObjectName("fullScreenBtn")
        self.status_badge = QLabel("Connecting")
        self.status_badge.setObjectName("statusBadge")
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.fullscreen_btn)
        head.addWidget(self.status_badge)

        self.stream_label = QLabel("Live Video Stream")
        self.stream_label.setAlignment(Qt.AlignCenter)
        self.stream_label.setObjectName("streamLabel")
        self.stream_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.stream_label.setMinimumSize(320, 240)
        self.stream_label.setScaledContents(True)

        self.stream_note = QLabel("Opening RTSP stream...")
        self.stream_note.setObjectName("streamNote")

        stream_layout.addLayout(head)
        stream_layout.addWidget(self.stream_label, 1)
        stream_layout.addWidget(self.stream_note)
        self.stream_panel = stream_panel

        control_panel = QFrame()
        control_panel.setObjectName("controlPanel")
        control_panel.setMinimumWidth(0)
        control_panel.setMaximumWidth(self.panel_expanded_width)
        self.control_panel = control_panel
        controls = QVBoxLayout(control_panel)
        controls.setContentsMargins(12, 12, 12, 12)
        controls.setSpacing(10)

        tabs = QTabWidget()
        tabs.addTab(self._build_control_tab(), "Control")
        tabs.addTab(self._build_settings_tab(), "Settings")
        controls.addWidget(tabs)

        self.panel_toggle_btn = QPushButton("❯")
        self.panel_toggle_btn.setObjectName("panelToggleBtn")
        self.panel_toggle_btn.setFixedWidth(24)

        self.panel_animation = QPropertyAnimation(self.control_panel, b"maximumWidth", self)
        self.panel_animation.setDuration(280)
        self.panel_animation.setEasingCurve(QEasingCurve.InOutCubic)

        shell.addWidget(stream_panel, 1)
        shell.addWidget(control_panel, 0)
        shell.addWidget(self.panel_toggle_btn, 0, alignment=Qt.AlignVCenter)

    def _build_control_tab(self):
        tab = QWidget()
        controls = QVBoxLayout(tab)
        controls.setContentsMargins(2, 2, 2, 2)
        controls.setSpacing(10)

        controls.addWidget(self._build_binary_switch("Control Mode", "Manual", "Auto", "mode"))
        controls.addWidget(self._build_binary_switch("View Mode", "Thermal", "Video", "view"))

        speed_group = QFrame()
        speed_group.setObjectName("group")
        speed_layout = QVBoxLayout(speed_group)
        speed_layout.setContentsMargins(10, 10, 10, 10)
        speed_layout.setSpacing(6)

        speed_title = QLabel("CAMERA SPEED")
        speed_title.setObjectName("groupTitle")

        speed_labels = QHBoxLayout()
        speed_labels.addWidget(QLabel("Low"))
        speed_labels.addStretch()
        speed_labels.addWidget(QLabel("High"))

        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(0, 100)
        self.speed_slider.setValue(self.state.speed)

        speed_layout.addWidget(speed_title)
        speed_layout.addLayout(speed_labels)
        speed_layout.addWidget(self.speed_slider)
        controls.addWidget(speed_group)

        telemetry = QFrame()
        telemetry.setObjectName("group")
        tele_layout = QGridLayout(telemetry)
        tele_layout.setContentsMargins(10, 10, 10, 10)
        tele_layout.setHorizontalSpacing(10)
        tele_layout.setVerticalSpacing(6)

        self.mode_val = QLabel()
        self.view_val = QLabel()
        self.speed_val = QLabel()
        self.move_val = QLabel()

        rows = [("Mode", self.mode_val), ("View", self.view_val), ("Speed", self.speed_val), ("Move", self.move_val)]
        for r, (k, v) in enumerate(rows):
            key = QLabel(k.upper())
            key.setObjectName("teleKey")
            v.setObjectName("teleVal")
            tele_layout.addWidget(key, r, 0)
            tele_layout.addWidget(v, r, 1, alignment=Qt.AlignRight)

        controls.addWidget(telemetry)
        controls.addWidget(self._build_recording_panel())
        controls.addStretch(1)
        controls.addWidget(self._build_dpad())
        return tab

    def _build_recording_panel(self):
        group = QFrame()
        group.setObjectName("group")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("RECORDING")
        title.setObjectName("groupTitle")

        row = QHBoxLayout()
        self.record_toggle_btn = QPushButton("Start Rec")
        self.take_image_btn = QPushButton("Take Image")
        row.addWidget(self.record_toggle_btn)
        row.addWidget(self.take_image_btn)

        self.record_status_label = QLabel("Recorder idle")
        self.record_status_label.setWordWrap(True)

        duration_row = QHBoxLayout()
        duration_label = QLabel("Segment (min)")
        duration_label.setObjectName("groupTitle")
        self.segment_duration_combo = QComboBox()
        self.segment_duration_combo.addItems(["1", "2", "5", "10", "15"])
        self.segment_duration_combo.setCurrentText(str(self.segment_minutes))
        duration_row.addWidget(duration_label)
        duration_row.addStretch()
        duration_row.addWidget(self.segment_duration_combo)

        layout.addWidget(title)
        layout.addLayout(row)
        layout.addLayout(duration_row)
        layout.addWidget(self.record_status_label)
        return group

    def _build_settings_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(10)

        url_group = QFrame()
        url_group.setObjectName("group")
        group_layout = QVBoxLayout(url_group)
        group_layout.setContentsMargins(10, 10, 10, 10)
        group_layout.setSpacing(8)

        title = QLabel("NETWORK SETTINGS")
        title.setObjectName("groupTitle")

        default_rtsp_label = QLabel(f"Default RTSP: {self.default_rtsp_url}")
        default_rtsp_label.setWordWrap(True)
        default_gimbal_label = QLabel(
            f"Default Gimbal TCP: {self.default_gimbal_ip}:{self.default_gimbal_port}"
        )
        default_gimbal_label.setWordWrap(True)

        input_label = QLabel("RTSP URL")
        input_label.setObjectName("groupTitle")
        self.rtsp_input = QLineEdit()
        self.rtsp_input.setPlaceholderText("Enter RTSP URL (leave empty to use default)")
        self.rtsp_input.setText(self.default_rtsp_url)

        gimbal_ip_label = QLabel("Gimbal IP")
        gimbal_ip_label.setObjectName("groupTitle")
        self.gimbal_ip_input = QLineEdit()
        self.gimbal_ip_input.setPlaceholderText("e.g. 192.168.2.119")
        self.gimbal_ip_input.setText(self.current_gimbal_ip)

        gimbal_port_label = QLabel("Gimbal Port")
        gimbal_port_label.setObjectName("groupTitle")
        self.gimbal_port_input = QLineEdit()
        self.gimbal_port_input.setValidator(QIntValidator(1, 65535, self))
        self.gimbal_port_input.setPlaceholderText("e.g. 2000")
        self.gimbal_port_input.setText(str(self.current_gimbal_port))

        btn_row = QHBoxLayout()
        self.apply_url_btn = QPushButton("Apply Network")
        self.use_default_btn = QPushButton("Use Defaults")
        btn_row.addWidget(self.apply_url_btn)
        btn_row.addWidget(self.use_default_btn)

        self.active_url_label = QLabel(f"Active URL: {self.current_rtsp_url}")
        self.active_url_label.setWordWrap(True)
        self.active_gimbal_label = QLabel(
            f"Active Gimbal TCP: {self.current_gimbal_ip}:{self.current_gimbal_port}"
        )
        self.active_gimbal_label.setWordWrap(True)
        self.settings_status_label = QLabel("Ready")
        self.settings_status_label.setWordWrap(True)

        group_layout.addWidget(title)
        group_layout.addWidget(default_rtsp_label)
        group_layout.addWidget(default_gimbal_label)
        group_layout.addWidget(input_label)
        group_layout.addWidget(self.rtsp_input)
        group_layout.addWidget(gimbal_ip_label)
        group_layout.addWidget(self.gimbal_ip_input)
        group_layout.addWidget(gimbal_port_label)
        group_layout.addWidget(self.gimbal_port_input)
        group_layout.addLayout(btn_row)
        group_layout.addWidget(self.active_url_label)
        group_layout.addWidget(self.active_gimbal_label)
        group_layout.addWidget(self.settings_status_label)

        layout.addWidget(url_group)
        layout.addStretch(1)
        return tab

    def apply_rtsp_url(self, input_url: str):
        url = input_url.strip() or self.default_rtsp_url
        should_restart = url != self.current_rtsp_url
        self.current_rtsp_url = url
        self.active_url_label.setText(f"Active URL: {url}")
        if should_restart:
            self.stream_note.setText(f"Opening RTSP: {url}")
            self.status_badge.setText("CONNECTING")
            self._restart_stream_reader()

    def use_default_rtsp_url(self):
        self.rtsp_input.setText(self.default_rtsp_url)
        self.apply_rtsp_url("")

    def apply_network_settings(self, rtsp_url: str, gimbal_ip: str, gimbal_port: str):
        ip = gimbal_ip.strip() or self.default_gimbal_ip
        try:
            port = int((gimbal_port or "").strip() or self.default_gimbal_port)
            if not (1 <= port <= 65535):
                raise ValueError("Port must be in range 1-65535")
        except ValueError:
            self.settings_status_label.setText("Invalid gimbal port. Use a number between 1 and 65535.")
            return

        self.apply_rtsp_url(rtsp_url)

        gimbal_changed = ip != self.current_gimbal_ip or port != self.current_gimbal_port
        if gimbal_changed:
            self._reconnect_gimbal(ip, port)
        else:
            self.settings_status_label.setText("Network settings applied.")

    def use_default_network_settings(self):
        self.rtsp_input.setText(self.default_rtsp_url)
        self.gimbal_ip_input.setText(self.default_gimbal_ip)
        self.gimbal_port_input.setText(str(self.default_gimbal_port))
        self.apply_network_settings("", self.default_gimbal_ip, str(self.default_gimbal_port))

    def _reconnect_gimbal(self, ip: str, port: int):
        try:
            self.gimbal.disconnect()
        except Exception:
            pass

        self.current_gimbal_ip = ip
        self.current_gimbal_port = port
        self.gimbal = ViewProGimbal(host=ip, port=port)
        self.active_gimbal_label.setText(f"Active Gimbal TCP: {ip}:{port}")
        self._connect_gimbal()

    def _restart_stream_reader(self):
        if self.reader is not None and self.reader.isRunning():
            self.reader.stop()

        self.reader = RtspReader(self.current_rtsp_url)
        self.reader.frame_ready.connect(self.actions.on_frame)
        self.reader.stream_status.connect(self.actions.on_stream_status)
        self.reader.start()

    def start_recording(self):
        if self.recorder.is_recording:
            return
        self.recorder.start()
        self.record_toggle_btn.setText("Stop Rec")
        self.record_status_label.setText(
            f"Recording... new file every {self.segment_minutes} min ({self.state.view.title()} mode)"
        )

    def stop_recording(self):
        if not self.recorder.is_recording:
            return
        self.recorder.stop()
        self.record_toggle_btn.setText("Start Rec")
        self.record_status_label.setText("Recorder stopped")

    def write_recording_frame(self, frame):
        if not self.recorder.is_recording:
            return
        current_file = self.recorder.write_frame(frame, self.state.view)
        if current_file is not None:
            self.record_status_label.setText(f"Recording: {current_file.name}")

    def set_segment_duration_minutes(self, minutes: int):
        self.segment_minutes = max(1, int(minutes))
        self.recorder.set_segment_seconds(self.segment_minutes * 60)
        if self.recorder.is_recording:
            self.record_status_label.setText(
                f"Recording... segment set to {self.segment_minutes} min"
            )
        else:
            self.record_status_label.setText(
                f"Recorder idle (segment: {self.segment_minutes} min)"
            )

    def update_latest_frame(self, frame):
        self.latest_frame = frame.copy()

    def take_snapshot(self):
        if self.latest_frame is None:
            self.record_status_label.setText("No frame available for snapshot")
            return

        snapshots_dir = Path(__file__).resolve().parent / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = f"{stamp}_{self.state.view.lower()}.jpg"
        path = snapshots_dir / file_name

        ok = cv2.imwrite(str(path), self.latest_frame)
        if ok:
            self.record_status_label.setText(f"Snapshot saved: {file_name}")
        else:
            self.record_status_label.setText("Snapshot save failed")

    def toggle_control_panel(self):
        target_visible = not self.panel_visible
        start_width = self.control_panel.maximumWidth()
        end_width = self.panel_expanded_width if target_visible else 0

        self.panel_animation.stop()
        self.panel_animation.setStartValue(start_width)
        self.panel_animation.setEndValue(end_width)
        self.panel_animation.start()

        self.panel_visible = target_visible
        self._update_panel_toggle_ui()

    def toggle_preview_fullscreen(self):
        if self._is_preview_fullscreen:
            self.exit_preview_fullscreen()
        else:
            self.enter_preview_fullscreen()

    def enter_preview_fullscreen(self):
        if self._is_preview_fullscreen:
            return
        self._panel_visible_before_fullscreen = self.panel_visible
        self._normal_shell_margins = self.shell_layout.contentsMargins()
        self._normal_shell_spacing = self.shell_layout.spacing()
        self._normal_stream_margins = self.stream_layout.contentsMargins()
        self._normal_stream_spacing = self.stream_layout.spacing()

        self.shell_layout.setContentsMargins(0, 0, 0, 0)
        self.shell_layout.setSpacing(0)
        self.stream_layout.setContentsMargins(0, 0, 0, 0)
        self.stream_layout.setSpacing(0)
        self.stream_panel.setStyleSheet("QFrame#streamPanel { border: none; border-radius: 0; background: #000000; }")
        self.stream_label.setStyleSheet("border: none; border-radius: 0; background: #000000;")
        self.stream_note.hide()
        self.control_panel.hide()
        self.panel_toggle_btn.hide()
        self.showFullScreen()
        self.fullscreen_btn.setText("Exit Full")
        self.fullscreen_btn.setToolTip("Exit full screen (Esc)")
        self._is_preview_fullscreen = True

    def exit_preview_fullscreen(self):
        if not self._is_preview_fullscreen:
            return
        self.showNormal()
        if self._normal_shell_margins is not None:
            self.shell_layout.setContentsMargins(self._normal_shell_margins)
        if self._normal_shell_spacing is not None:
            self.shell_layout.setSpacing(self._normal_shell_spacing)
        if self._normal_stream_margins is not None:
            self.stream_layout.setContentsMargins(self._normal_stream_margins)
        if self._normal_stream_spacing is not None:
            self.stream_layout.setSpacing(self._normal_stream_spacing)
        self.stream_panel.setStyleSheet("")
        self.stream_label.setStyleSheet("")
        self.stream_note.show()
        self.control_panel.show()
        self.panel_toggle_btn.show()
        if self._panel_visible_before_fullscreen:
            self.control_panel.setMaximumWidth(self.panel_expanded_width)
            self.panel_visible = True
        else:
            self.control_panel.setMaximumWidth(0)
            self.panel_visible = False
        self._update_panel_toggle_ui()
        self.fullscreen_btn.setText("Full Screen")
        self.fullscreen_btn.setToolTip("Enter full screen (F)")
        self._is_preview_fullscreen = False

    def _update_panel_toggle_ui(self):
        if self.panel_visible:
            self.panel_toggle_btn.setText("❯")
            self.panel_toggle_btn.setToolTip("Hide Control/Settings Panel")
        else:
            self.panel_toggle_btn.setText("❮")
            self.panel_toggle_btn.setToolTip("Show Control/Settings Panel")

    def _build_binary_switch(self, title_text, left_text, right_text, kind):
        group = QFrame()
        group.setObjectName("group")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)

        title = QLabel(title_text.upper())
        title.setObjectName("groupTitle")
        layout.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(8)

        left = QPushButton(left_text.upper())
        right = QPushButton(right_text.upper())
        for btn in (left, right):
            btn.setCheckable(True)
            btn.setObjectName("switchBtn")
            btn.setMinimumHeight(34)

        group_btn = QButtonGroup(self)
        group_btn.setExclusive(True)
        group_btn.addButton(left)
        group_btn.addButton(right)

        if kind == "mode":
            self.mode_left_btn, self.mode_right_btn = left, right
            self.mode_group = group_btn
        else:
            self.view_left_btn, self.view_right_btn = left, right
            self.view_group = group_btn

        row.addWidget(left)
        row.addWidget(right)
        layout.addLayout(row)
        return group

    def _build_dpad(self):
        group = QFrame()
        group.setObjectName("group")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("MOVEMENT CONTROLLER")
        title.setObjectName("groupTitle")
        layout.addWidget(title)

        dpad_wrap = QFrame()
        dpad_wrap.setObjectName("dpadWrap")
        grid = QGridLayout(dpad_wrap)
        grid.setContentsMargins(14, 14, 14, 14)
        grid.setSpacing(10)

        self.btn_up = QPushButton("▲")
        self.btn_right = QPushButton("▶")
        self.btn_down = QPushButton("▼")
        self.btn_left = QPushButton("◀")
        self.btn_home = QPushButton("H")

        for b in (self.btn_up, self.btn_right, self.btn_down, self.btn_left, self.btn_home):
            b.setObjectName("dpadBtn")
            b.setFixedSize(48, 48)

        self.btn_home.setObjectName("homeBtn")

        grid.addWidget(self.btn_up, 0, 1)
        grid.addWidget(self.btn_left, 1, 0)
        grid.addWidget(self.btn_home, 1, 1)
        grid.addWidget(self.btn_right, 1, 2)
        grid.addWidget(self.btn_down, 2, 1)

        layout.addWidget(dpad_wrap, alignment=Qt.AlignCenter)
        return group

    def _apply_theme(self):
        self.setStyleSheet(
            """
            QWidget {
                background: #effffb;
                color: #10252b;
                font-family: 'DejaVu Sans';
                font-size: 13px;
            }
            QFrame {
                background: rgba(255,255,255,0.86);
                border: 1px solid rgba(16,72,78,0.18);
                border-radius: 18px;
            }
            #controlPanel {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(255,255,255,0.92), stop:1 rgba(225,255,246,0.92));
            }
            #title {
                font-size: 22px;
                font-weight: 700;
                letter-spacing: 2.4px;
                background: transparent;
                border: none;
                color: #0c3a40;
            }
            #statusBadge {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #00c7a5, stop:1 #00a7d6);
                color: white;
                padding: 5px 12px;
                border-radius: 999px;
                border: none;
                font-weight: 700;
            }
            #streamLabel {
                border: 1px solid rgba(13,70,78,0.25);
                border-radius: 16px;
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #0b1e22, stop:1 #14343c);
                color: #bfe8ef;
                font-size: 28px;
            }
            #streamNote {
                background: transparent;
                border: none;
                color: #2f6570;
                font-size: 12px;
                min-height: 18px;
            }
            #group {
                border-radius: 16px;
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(255,255,255,0.95), stop:1 rgba(233,255,248,0.9));
            }
            #groupTitle {
                font-weight: 700;
                font-size: 12px;
                letter-spacing: 1.5px;
                color: #2f5f67;
                background: transparent;
                border: none;
            }
            #switchBtn {
                border-radius: 999px;
                border: none;
                padding: 8px;
                font-weight: 700;
                letter-spacing: 1px;
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #d9fff0, stop:1 #fff5dd);
                color: #11424a;
            }
            #switchBtn:checked {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #ff8f4f, stop:1 #ffd166);
                color: #261100;
            }
            QSlider::groove:horizontal {
                height: 8px;
                background: #2f474d;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #ff7a2f, stop:1 #ffb347);
                width: 18px;
                margin: -5px 0;
                border-radius: 8px;
            }
            #teleKey {
                color: #467178;
                font-weight: 700;
                letter-spacing: 1px;
                background: transparent;
                border: none;
            }
            #teleVal {
                color: #0f2f36;
                font-weight: 700;
                background: transparent;
                border: none;
            }
            #dpadWrap {
                border-radius: 90px;
                background: qradialgradient(cx:0.35, cy:0.25, radius:0.9, stop:0 #f8fffd, stop:1 #baf2e3);
            }
            #dpadBtn, #homeBtn {
                border: none;
                border-radius: 14px;
                font-size: 22px;
                font-weight: 700;
                background: #edf7f6;
                color: #285f67;
            }
            #dpadBtn:pressed, #homeBtn:pressed {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #ff9f63, stop:1 #ffd16f);
                color: #31200a;
            }
            #homeBtn {
                border-radius: 24px;
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #00bda2, stop:1 #00a8d8);
                color: white;
                font-size: 16px;
            }
            QTabWidget::pane {
                border: 1px solid rgba(16,72,78,0.2);
                border-radius: 14px;
                background: rgba(255,255,255,0.72);
                top: -1px;
            }
            QTabBar::tab {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #d7fff0, stop:1 #fff2d3);
                color: #1f4a52;
                border: 1px solid rgba(16,72,78,0.16);
                padding: 8px 12px;
                margin-right: 6px;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
                font-weight: 700;
            }
            QTabBar::tab:selected {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #ff9957, stop:1 #ffd26a);
                color: #2a1401;
            }
            QLineEdit {
                border: 1px solid rgba(16,72,78,0.25);
                border-radius: 10px;
                padding: 8px;
                background: white;
                color: #163b42;
            }
            QPushButton {
                border: none;
                border-radius: 10px;
                padding: 8px 10px;
                font-weight: 700;
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #d8fff0, stop:1 #fff1d2);
                color: #1d4950;
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #ff9152, stop:1 #ffd069);
                color: #2f1804;
            }
            #panelToggleBtn {
                border: 1px solid rgba(16,72,78,0.22);
                border-radius: 12px;
                min-height: 64px;
                max-height: 64px;
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #00c6a6, stop:1 #00a7d6);
                color: #ffffff;
                font-size: 16px;
                font-weight: 800;
            }
            #panelToggleBtn:pressed {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #ff9152, stop:1 #ffd069);
                color: #2f1804;
            }
            #fullScreenBtn {
                border-radius: 10px;
                min-height: 32px;
                padding: 6px 12px;
            }
            """
        )

    def closeEvent(self, event):
        if self._is_preview_fullscreen:
            self.exit_preview_fullscreen()
        if hasattr(self, "recorder"):
            self.recorder.stop()
        if hasattr(self, "reader") and self.reader.isRunning():
            self.reader.stop()
        if hasattr(self, "gimbal"):
            self.gimbal.disconnect()
        super().closeEvent(event)

    def eventFilter(self, watched, event):
        if event.type() not in (QEvent.ShortcutOverride, QEvent.KeyPress, QEvent.KeyRelease):
            return super().eventFilter(watched, event)

        if isinstance(self.focusWidget(), QLineEdit):
            return super().eventFilter(watched, event)

        key_event = event if isinstance(event, QKeyEvent) else None
        if key_event is None:
            return super().eventFilter(watched, event)

        handled_keys = {Qt.Key_Up, Qt.Key_Right, Qt.Key_Down, Qt.Key_Left, Qt.Key_F, Qt.Key_Escape}
        is_ctrl_h = key_event.key() == Qt.Key_H and (key_event.modifiers() & Qt.ControlModifier)

        if event.type() == QEvent.ShortcutOverride and (key_event.key() in handled_keys or is_ctrl_h):
            event.accept()
            return True

        if key_event.type() == QEvent.KeyPress:
            if key_event.isAutoRepeat():
                return True
            return self._handle_key_press(key_event)

        if key_event.isAutoRepeat():
            return True
        return self._handle_key_release(key_event)

    def _handle_key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key_F and event.modifiers() == Qt.NoModifier:
            self.toggle_preview_fullscreen()
            return True

        if event.key() == Qt.Key_Escape and self._is_preview_fullscreen:
            self.exit_preview_fullscreen()
            return True

        key_to_move = {
            Qt.Key_Up: "up",
            Qt.Key_Right: "right",
            Qt.Key_Down: "down",
            Qt.Key_Left: "left",
        }

        move = key_to_move.get(event.key())
        if move:
            if move not in self._active_keyboard_moves:
                self._active_keyboard_moves.add(move)
                self.actions.on_move_pressed(move)
            return True

        if event.key() == Qt.Key_H and (event.modifiers() & Qt.ControlModifier):
            self.actions.on_move_pressed("home")
            return True

        return False

    def _handle_key_release(self, event: QKeyEvent) -> bool:
        key_to_move = {
            Qt.Key_Up: "up",
            Qt.Key_Right: "right",
            Qt.Key_Down: "down",
            Qt.Key_Left: "left",
        }

        move = key_to_move.get(event.key())
        if move:
            self._active_keyboard_moves.discard(move)
            self.actions.on_move_released(move)
            return True

        return False


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("DejaVu Sans", 10))

    win = ControllerWindow()
    win.show()

    sys.exit(app.exec())
