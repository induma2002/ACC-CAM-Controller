# Python Desktop App (No Extra Service)

This app opens the RTSP camera directly in a desktop window.

Configured RTSP URL:

- `rtsp://192.168.87.49:8554/h264`

## 1) Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

## 2) Run

```bash
python3 desktop_app.py
```

## Notes

- No Docker and no relay service required.
- If stream does not open, confirm camera IP/network and RTSP path.
- For lower latency, use wired LAN if possible.
