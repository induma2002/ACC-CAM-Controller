from functools import partial

import cv2
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QPushButton


class ControllerActions:
    def __init__(self, window):
        self.window = window
        self.repeat_timer = QTimer(self.window)
        self.repeat_timer.setInterval(120)
        self.repeat_timer.timeout.connect(self._emit_repeat_move)
        self.active_move = None

    def bind_events(self):
        w = self.window

        w.mode_left_btn.clicked.connect(self.on_mode_manual_clicked)
        w.mode_right_btn.clicked.connect(self.on_mode_auto_clicked)

        w.view_left_btn.clicked.connect(self.on_view_thermal_clicked)
        w.view_right_btn.clicked.connect(self.on_view_video_clicked)

        w.speed_slider.valueChanged.connect(self.on_speed_changed)
        w.apply_url_btn.clicked.connect(self.on_apply_rtsp_clicked)
        w.use_default_btn.clicked.connect(self.on_use_default_rtsp_clicked)

        self.bind_move_button(w.btn_up, "up")
        self.bind_move_button(w.btn_right, "right")
        self.bind_move_button(w.btn_down, "down")
        self.bind_move_button(w.btn_left, "left")
        self.bind_move_button(w.btn_home, "home")

    def bind_move_button(self, button: QPushButton, move_value: str):
        button.pressed.connect(partial(self.on_move_pressed, move_value))
        button.released.connect(partial(self.on_move_released, move_value))

    # Action methods: add your real camera/PTZ code inside these methods later.
    def on_mode_manual_clicked(self):
        self.log_action("mode_manual_clicked")

    def on_mode_auto_clicked(self):
        self.log_action("mode_auto_clicked")

    def on_view_thermal_clicked(self):
        self.log_action("view_thermal_clicked")

    def on_view_video_clicked(self):
        self.log_action("view_video_clicked")

    def on_speed_changed(self, value: int):
        self.log_action(f"speed_changed:{value}")

    def on_apply_rtsp_clicked(self):
        url = self.window.rtsp_input.text().strip()
        effective = url or self.window.default_rtsp_url
        self.log_action(f"apply_rtsp_clicked:{effective}")
        self.window.apply_rtsp_url(url)

    def on_use_default_rtsp_clicked(self):
        self.log_action("use_default_rtsp_clicked")
        self.window.use_default_rtsp_url()

    def on_move_pressed(self, move_value: str):
        self.log_action(f"move_pressed:{move_value}")
        if move_value in {"up", "right", "down", "left"}:
            self.active_move = move_value
            if not self.repeat_timer.isActive():
                self.repeat_timer.start()

    def on_move_released(self, move_value: str):
        self.log_action(f"move_released:{move_value}")
        if self.active_move == move_value:
            self.active_move = None
            self.repeat_timer.stop()

    def _emit_repeat_move(self):
        if self.active_move:
            self.log_action(f"move_repeat:{self.active_move}")

    def log_action(self, action: str):
        print(f"[UI_ACTION] {action}", flush=True)

    def on_stream_status(self, ok: bool, message: str):
        w = self.window
        if ok:
            w.status_badge.setText("LIVE")
            w.stream_note.setText("")
        else:
            w.status_badge.setText("STREAM OFF")
            w.stream_note.setText(message)

    def on_frame(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self.window.stream_label.setPixmap(QPixmap.fromImage(qimg))

    def render_state(self):
        w = self.window
        w.mode_left_btn.setChecked(w.state.mode == "manual")
        w.mode_right_btn.setChecked(w.state.mode == "auto")
        w.view_left_btn.setChecked(w.state.view == "thermal")
        w.view_right_btn.setChecked(w.state.view == "video")

        w.mode_val.setText(w.state.mode.title())
        w.view_val.setText(w.state.view.title())
        w.speed_val.setText(f"{w.state.speed}%")
        w.move_val.setText(w.state.movement.title())

        if w.status_badge.text() == "LIVE":
            w.status_badge.setText("AUTO" if w.state.mode == "auto" else "MANUAL")
