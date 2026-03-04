import cv2
import subprocess
import json
import os
import csv
import time
from datetime import datetime

from constants import DEFAULT_RTSP_URL, SCANNER_LOG_DIR

LOG_DIR = SCANNER_LOG_DIR
os.makedirs(LOG_DIR, exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
CSV_FILE = os.path.join(LOG_DIR, f"rtsp_scan_{timestamp}.csv")


def print_section(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def get_ffprobe_data(rtsp_url):
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-rtsp_transport", "tcp",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        rtsp_url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception as e:
        return {"error": str(e)}
    return None


def measure_latency(rtsp_url):
    start = time.time()
    cap = cv2.VideoCapture(rtsp_url)
    opened = cap.isOpened()
    end = time.time()
    cap.release()
    return opened, round((end - start) * 1000, 2)


def monitor_stream(rtsp_url, duration=10):
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        return None

    start_time = time.time()
    frame_count = 0
    total_bytes = 0

    while time.time() - start_time < duration:
        ret, frame = cap.read()
        if not ret:
            break
        frame_count += 1
        total_bytes += frame.nbytes

    cap.release()

    elapsed = time.time() - start_time
    real_fps = frame_count / elapsed if elapsed > 0 else 0
    bitrate_mbps = (total_bytes * 8) / (elapsed * 1_000_000) if elapsed > 0 else 0

    return {
        "frames_captured": frame_count,
        "monitor_duration_sec": round(elapsed, 2),
        "real_fps": round(real_fps, 2),
        "estimated_bitrate_mbps": round(bitrate_mbps, 2)
    }


def scan_rtsp(rtsp_url):
    print_section("RTSP CONNECTION TEST")

    opened, latency = measure_latency(rtsp_url)

    if not opened:
        print("❌ Connection failed.")
        return None

    print(f"✅ Connected successfully")
    print(f"⏱  Connection latency : {latency} ms")

    cap = cv2.VideoCapture(rtsp_url)
    ret, frame = cap.read()

    details = {
        "timestamp": datetime.now().isoformat(),
        "rtsp_url": rtsp_url,
        "connection_latency_ms": latency,
        "resolution_width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "resolution_height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "reported_fps": cap.get(cv2.CAP_PROP_FPS),
        "backend": cap.getBackendName(),
        "brightness": cap.get(cv2.CAP_PROP_BRIGHTNESS),
        "contrast": cap.get(cv2.CAP_PROP_CONTRAST),
        "saturation": cap.get(cv2.CAP_PROP_SATURATION),
        "fourcc_raw": int(cap.get(cv2.CAP_PROP_FOURCC)),
    }

    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    details["fourcc_string"] = "".join(
        [chr((fourcc >> 8 * i) & 0xFF) for i in range(4)]
    )

    if ret and frame is not None:
        details["frame_shape"] = str(frame.shape)
        details["frame_dtype"] = str(frame.dtype)
        details["frame_size_bytes"] = frame.nbytes

    print_section("OPENCV STREAM DETAILS")
    for k, v in details.items():
        print(f"{k:<30} : {v}")

    cap.release()

    print_section("LIVE STREAM MONITOR (10s)")
    monitor_data = monitor_stream(rtsp_url, duration=10)
    if monitor_data:
        for k, v in monitor_data.items():
            print(f"{k:<30} : {v}")
        details.update(monitor_data)
    else:
        print("⚠️  Monitor failed.")

    print_section("FFPROBE METADATA")
    ffprobe_data = get_ffprobe_data(rtsp_url)

    if ffprobe_data and "streams" in ffprobe_data:
        stream = ffprobe_data["streams"][0]

        fields = [
            "codec_name", "codec_long_name",
            "profile", "level",
            "width", "height",
            "pix_fmt",
            "r_frame_rate", "avg_frame_rate",
            "bit_rate", "nb_frames", "duration"
        ]

        for f in fields:
            value = stream.get(f, "")
            print(f"{f:<30} : {value}")
            details[f"ffprobe_{f}"] = value
    else:
        print("⚠️  FFprobe failed or not installed.")

    return details


def write_to_csv(data):
    if not data:
        return

    file_exists = os.path.isfile(CSV_FILE)

    with open(CSV_FILE, mode="a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=data.keys())

        if not file_exists:
            writer.writeheader()

        writer.writerow(data)

    print_section("LOGGING")
    print(f"📁 Data written to: {CSV_FILE}")


if __name__ == "__main__":
    RTSP_URL = DEFAULT_RTSP_URL

    result = scan_rtsp(RTSP_URL)
    write_to_csv(result)
