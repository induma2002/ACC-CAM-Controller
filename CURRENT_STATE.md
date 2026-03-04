# Current Project State

## Project Type
Python desktop application for thermal camera control UI.

## Main Files
- `desktop_app.py`: Main window, UI layout, RTSP reader thread, settings tab, panel hide/show animation.
- `controller_actions.py`: All UI action methods and event bindings.
- `requirements.txt`: Python dependencies (`PySide6`, `opencv-python`).
- `DESKTOP_RUN.md`: Run instructions.

## Default Stream
- Hardcoded default RTSP URL:
  - `rtsp://192.168.87.49:8554/h264`

## Implemented Features
- Live RTSP video display in desktop app.
- Reconnect loop on stream failure/timeouts/errors.
- Safer thread shutdown to avoid `QThread destroyed while running` crash.
- Control panel with tabs:
  - `Control`
  - `Settings`
- Settings tab:
  - Manual RTSP URL override input
  - `Apply URL` button (overrides default)
  - `Use Default` button (restores hardcoded URL)
- Control/Settings side panel hide/show with animated slide.
- Movement buttons (`up/right/down/left`) repeat continuously while pressed.
- Movement stops immediately when button is released.
- Center button is `H` (home).
- All control actions are currently logging-only (no hardware command integration yet).

## Action Method Placeholders (for future real logic)
In `controller_actions.py`:
- `on_mode_manual_clicked`
- `on_mode_auto_clicked`
- `on_view_thermal_clicked`
- `on_view_video_clicked`
- `on_speed_changed`
- `on_move_pressed`
- `on_move_released`
- `on_apply_rtsp_clicked`
- `on_use_default_rtsp_clicked`
- `on_panel_toggle_clicked`

## Current Behavior Note
UI controls print action logs to console (e.g. `[UI_ACTION] ...`).
No PTZ/camera API commands are sent yet.
