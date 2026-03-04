import queue
import socket
import threading
import time
from logging import Formatter, INFO, Logger, getLogger
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from constants import (
    DEFAULT_CAMERA_IP,
    DEFAULT_GIMBAL_PORT,
    DEFAULT_SOCKET_TIMEOUT,
    GIMBAL_CENTER_PWM,
    GIMBAL_CHECKSUM_NEGATIVE,
    GIMBAL_CHECKSUM_POSITIVE,
    GIMBAL_HEARTBEATS,
    GIMBAL_HOME_PACKET,
    GIMBAL_MAX_PWM,
    GIMBAL_MIN_PWM,
    GIMBAL_MOVE_BASE_PACKET,
    GIMBAL_STOP_PACKET,
    GIMBAL_VIEW_MODES,
)


class ViewProGimbal:
    HOST = DEFAULT_CAMERA_IP
    PORT = DEFAULT_GIMBAL_PORT
    CENTER_PWM = GIMBAL_CENTER_PWM
    MIN_PWM = GIMBAL_MIN_PWM
    MAX_PWM = GIMBAL_MAX_PWM

    _HEARTBEATS = GIMBAL_HEARTBEATS

    _MOVE_BASE = GIMBAL_MOVE_BASE_PACKET
    _STOP_PACKET = GIMBAL_STOP_PACKET
    _HOME_PACKET = GIMBAL_HOME_PACKET
    MODES = GIMBAL_VIEW_MODES
    _CHECKSUM_POSITIVE = GIMBAL_CHECKSUM_POSITIVE
    _CHECKSUM_NEGATIVE = GIMBAL_CHECKSUM_NEGATIVE

    def __init__(self, host: str = HOST, port: int = PORT):
        self.host = host
        self.port = port
        self._logger = self._build_logger()
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._send_queue: "queue.Queue[tuple[str, bytes]]" = queue.Queue(maxsize=1)
        self._running = threading.Event()
        self._tx_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

    @property
    def is_connected(self) -> bool:
        return self._running.is_set() and self._sock is not None

    def connect(self):
        if self.is_connected:
            self._logger.info("connect skipped: already connected")
            return

        self._logger.info("connecting to gimbal %s:%s", self.host, self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(DEFAULT_SOCKET_TIMEOUT)
        try:
            sock.connect((self.host, self.port))
            sock.settimeout(1.0)
        except Exception:
            self._logger.exception("gimbal connect failed")
            sock.close()
            raise

        self._sock = sock
        self._running.set()
        self._tx_thread = threading.Thread(target=self._tx_loop, name="viewpro-tx", daemon=True)
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="viewpro-heartbeat", daemon=True)
        self._tx_thread.start()
        self._heartbeat_thread.start()
        self._logger.info("gimbal connected")

    def disconnect(self):
        self._logger.info("disconnect requested")
        self._running.clear()

        self._enqueue_packet("stop", self._STOP_PACKET)

        if self._tx_thread and self._tx_thread.is_alive():
            self._tx_thread.join(timeout=1.5)
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=1.5)

        with self._send_lock:
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._sock.close()
                self._sock = None
        self._logger.info("gimbal disconnected")

    def move(self, yaw: int, pitch: int):
        if not self.is_connected:
            self._logger.warning("move skipped: not connected")
            return

        yaw = self._clamp(yaw, -100, 100)
        pitch = self._clamp(pitch, -100, 100)

        if yaw != 0 and pitch != 0:
            if abs(yaw) >= abs(pitch):
                pitch = 0
            else:
                yaw = 0

        if yaw == 0 and pitch == 0:
            self.stop()
            return

        yaw_pwm = self._to_pwm(yaw)
        pitch_pwm = self._to_pwm(pitch)
        packet = bytearray(self._MOVE_BASE)
        packet[11:13] = yaw_pwm.to_bytes(2, "big")
        packet[15:17] = pitch_pwm.to_bytes(2, "big")

        # Device-provided checksum values for signed cardinal movement packets.
        if yaw < 0 or pitch < 0:
            packet[-2:] = self._CHECKSUM_NEGATIVE
        else:
            packet[-2:] = self._CHECKSUM_POSITIVE

        self._enqueue_packet("move", bytes(packet))

    def stop(self):
        if not self.is_connected:
            self._logger.warning("stop skipped: not connected")
            return
        self._enqueue_packet("stop", self._STOP_PACKET)

    def home(self):
        if not self.is_connected:
            self._logger.warning("home skipped: not connected")
            return
        self._enqueue_packet("home", self._HOME_PACKET)

    def set_view_mode(self, mode: str):
        if not self.is_connected:
            self._logger.warning("set_view_mode skipped (%s): not connected", mode)
            return
        packet = self.MODES.get(mode)
        if packet is None:
            self._logger.warning("set_view_mode skipped: unknown mode '%s'", mode)
            return
        self._enqueue_packet(f"view_{mode}", packet)

    def _to_pwm(self, value: int) -> int:
        if value == 0:
            return self.CENTER_PWM

        if value > 0:
            return self.CENTER_PWM + int((self.MAX_PWM - self.CENTER_PWM) * (abs(value) / 100.0))

        return self.CENTER_PWM - int((self.CENTER_PWM - self.MIN_PWM) * (abs(value) / 100.0))

    def _enqueue_packet(self, packet_type: str, packet: bytes):
        if not self.is_connected:
            self._logger.warning("enqueue skipped (%s): not connected", packet_type)
            return

        try:
            while self._send_queue.full():
                self._send_queue.get_nowait()
                self._logger.warning("queue full: dropped oldest packet")
            self._send_queue.put_nowait((packet_type, packet))
        except queue.Empty:
            pass
        except queue.Full:
            self._logger.warning("queue full: failed to enqueue packet")

    def _heartbeat_loop(self):
        idx = 0
        while self._running.is_set():
            self._send_packet("heartbeat", self._HEARTBEATS[idx % len(self._HEARTBEATS)])
            idx += 1
            time.sleep(0.1)

    def _tx_loop(self):
        while self._running.is_set():
            try:
                packet_type, packet = self._send_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._send_packet(packet_type, packet)

    def _send_packet(self, packet_type: str, packet: bytes):
        with self._send_lock:
            if self._sock is None:
                self._logger.warning("send skipped (%s): socket is not available", packet_type)
                return
            try:
                self._sock.sendall(packet)
                self._logger.info("packet sent (%s): %s", packet_type, packet.hex())
            except OSError:
                self._logger.exception("packet send failed (%s)", packet_type)
                self._running.clear()

    @staticmethod
    def _clamp(value: int, low: int, high: int) -> int:
        return max(low, min(high, int(value)))

    @staticmethod
    def _build_logger() -> Logger:
        logger = getLogger("viewpro_gimbal")
        if logger.handlers:
            return logger

        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "gimbal_driver.log"

        handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(Formatter("%(asctime)s | %(levelname)s | %(message)s"))

        logger.setLevel(INFO)
        logger.addHandler(handler)
        logger.propagate = False
        return logger
