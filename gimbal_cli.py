import socket
import threading
import time
import sys

from constants import (
    CLI_HEARTBEATS as HEARTBEATS,
    CLI_MODES as MODES,
    DEFAULT_CAMERA_IP as CAMERA_IP,
    DEFAULT_GIMBAL_PORT as CAMERA_PORT,
    DEFAULT_SOCKET_TIMEOUT as TIMEOUT,
)

stop_event = threading.Event()


def recv_loop(sock: socket.socket):
    while not stop_event.is_set():
        try:
            data = sock.recv(4096)
            if not data:
                print("Camera closed connection.")
                stop_event.set()
                break
            print(f"[RX] {data.hex(' ').upper()}")
        except socket.timeout:
            pass
        except OSError:
            break


def heartbeat_loop(sock: socket.socket, interval=0.15):
    i = 0
    while not stop_event.is_set():
        try:
            hb = HEARTBEATS[i % len(HEARTBEATS)]
            sock.sendall(hb)
            i += 1
        except OSError:
            stop_event.set()
            break
        time.sleep(interval)


def send_mode(sock: socket.socket, mode: str):
    if mode not in MODES:
        print("Unknown mode. Use: visible / ir")
        return
    packet = MODES[mode]
    print(f"[TX MODE] {mode.upper()}")
    sock.sendall(packet)


def main():
    global stop_event
    stop_event.clear()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(TIMEOUT)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        print(f"Connecting to {CAMERA_IP}:{CAMERA_PORT} ...")
        sock.connect((CAMERA_IP, CAMERA_PORT))
        print("Connected.")

        # Start threads
        t_rx = threading.Thread(target=recv_loop, args=(sock,), daemon=True)
        t_hb = threading.Thread(target=heartbeat_loop, args=(sock,), daemon=True)

        t_rx.start()
        t_hb.start()

        time.sleep(1.0)  # allow heartbeat to stabilize session

        print("\nType commands: visible / ir / quit\n")

        while True:
            cmd = input(">> ").strip().lower()

            if cmd == "quit":
                break
            elif cmd in ("visible", "ir"):
                send_mode(sock, cmd)
            else:
                print("Commands: visible / ir / quit")

        stop_event.set()
        time.sleep(0.2)

    print("Disconnected.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
