import os
import sys
import time
from dataclasses import dataclass

import cv2
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
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

CAMERA_RTSP_URL = "rtsp://192.168.87.49:8554/h264"


@dataclass
class AppState:
    mode: str = "auto"
    view: str = "thermal"
    movement: str = "idle"
    speed: int = 35


class RtspReader(QThread):
    frame_ready = Signal(object)
    stream_status = Signal(bool, str)

    def __init__(self, rtsp_url: str):
        super().__init__()
        self.rtsp_url = rtsp_url
        self._running = True

    def stop(self):
        self._running = False
        self.requestInterruption()
        if not self.wait(35000):
            # Last-resort guard to avoid "QThread destroyed while thread is still running".
            self.terminate()
            self.wait(2000)

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
                if cap is not None:
                    cap.release()

            if self._running and not self.isInterruptionRequested():
                time.sleep(reconnect_delay)


class ControllerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Thermal Camera Controller")
        self.resize(1280, 760)

        self.state = AppState()
        self.default_rtsp_url = CAMERA_RTSP_URL
        self.current_rtsp_url = CAMERA_RTSP_URL
        self.reader = None

        self._build_ui()
        self.actions = ControllerActions(self)
        self.actions.bind_events()
        self._apply_theme()
        self.actions.render_state()
        self._restart_stream_reader()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)

        shell = QHBoxLayout(root)
        shell.setContentsMargins(14, 14, 14, 14)
        shell.setSpacing(14)

        stream_panel = QFrame()
        stream_layout = QVBoxLayout(stream_panel)
        stream_layout.setContentsMargins(12, 12, 12, 12)
        stream_layout.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("THERMAL CAMERA")
        title.setObjectName("title")
        self.status_badge = QLabel("Connecting")
        self.status_badge.setObjectName("statusBadge")
        head.addWidget(title)
        head.addStretch()
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

        control_panel = QFrame()
        control_panel.setObjectName("controlPanel")
        controls = QVBoxLayout(control_panel)
        controls.setContentsMargins(12, 12, 12, 12)
        controls.setSpacing(10)

        tabs = QTabWidget()
        tabs.addTab(self._build_control_tab(), "Control")
        tabs.addTab(self._build_settings_tab(), "Settings")
        controls.addWidget(tabs)

        shell.addWidget(stream_panel, 1)
        shell.addWidget(control_panel, 0)

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
        controls.addStretch(1)
        controls.addWidget(self._build_dpad())
        return tab

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

        title = QLabel("STREAM SETTINGS")
        title.setObjectName("groupTitle")

        default_label = QLabel(f"Default RTSP: {self.default_rtsp_url}")
        default_label.setWordWrap(True)

        input_label = QLabel("RTSP URL Override")
        input_label.setObjectName("groupTitle")
        self.rtsp_input = QLineEdit()
        self.rtsp_input.setPlaceholderText("Enter RTSP URL (leave empty to use default)")
        self.rtsp_input.setText(self.default_rtsp_url)

        btn_row = QHBoxLayout()
        self.apply_url_btn = QPushButton("Apply URL")
        self.use_default_btn = QPushButton("Use Default")
        btn_row.addWidget(self.apply_url_btn)
        btn_row.addWidget(self.use_default_btn)

        self.active_url_label = QLabel(f"Active URL: {self.current_rtsp_url}")
        self.active_url_label.setWordWrap(True)

        group_layout.addWidget(title)
        group_layout.addWidget(default_label)
        group_layout.addWidget(input_label)
        group_layout.addWidget(self.rtsp_input)
        group_layout.addLayout(btn_row)
        group_layout.addWidget(self.active_url_label)

        layout.addWidget(url_group)
        layout.addStretch(1)
        return tab

    def apply_rtsp_url(self, input_url: str):
        url = input_url.strip() or self.default_rtsp_url
        self.current_rtsp_url = url
        self.active_url_label.setText(f"Active URL: {url}")
        self.stream_note.setText(f"Opening RTSP: {url}")
        self.status_badge.setText("CONNECTING")
        self._restart_stream_reader()

    def use_default_rtsp_url(self):
        self.rtsp_input.setText(self.default_rtsp_url)
        self.apply_rtsp_url("")

    def _restart_stream_reader(self):
        if self.reader is not None and self.reader.isRunning():
            self.reader.stop()

        self.reader = RtspReader(self.current_rtsp_url)
        self.reader.frame_ready.connect(self.actions.on_frame)
        self.reader.stream_status.connect(self.actions.on_stream_status)
        self.reader.start()

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
                min-width: 330px;
                max-width: 330px;
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
            """
        )

    def closeEvent(self, event):
        if hasattr(self, "reader") and self.reader.isRunning():
            self.reader.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("DejaVu Sans", 10))

    win = ControllerWindow()
    win.show()

    sys.exit(app.exec())
