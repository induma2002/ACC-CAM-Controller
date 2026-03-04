import socket
import time

# Target IP and Port (change if different)
TARGET_IP = "192.168.2.119"
TARGET_PORT = 2000

# Raw TCP payload (ONLY the control packet, not full Wireshark frame)
payload = bytes.fromhex(
    "EB 90 14 55 AA DC 11 30 0F 00 "
    "00 00 00 00 00 00 00 03 "
    "82 00 00 00 AF 5F"
)

try:
    # Create TCP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((TARGET_IP, TARGET_PORT))

    print(f"[+] Connected to {TARGET_IP}:{TARGET_PORT}")

    while True:
        sock.sendall(payload)
        print("[+] Packet sent")
        time.sleep(0.15)

except Exception as e:
    print("[-] Error:", e)

finally:
    sock.close()