"""UDP relay: ESP32 (WiFi) -> this PC -> FPGA board (169.254 link-local).

Why this exists
---------------
The board's IP is hardcoded to 169.254.191.50 and sits on a direct Ethernet link
to this PC.  An ESP32 on WiFi gets a 192.168.x.x address and therefore cannot
reach the board's subnet at all.  This script bridges the two without touching
the firmware: it listens on one port and forwards the payload to the board's
control port (50507).

The board's control plane takes a single ASCII byte:
    '0' bypass | '1' RNNoise | '2' light gate
    't' toggle self-test tone | 'a'..'d' noise floor 0/300/800/1500

Usage
-----
    python udp_relay.py                 # listen 0.0.0.0:50508 -> board:50507
    python udp_relay.py --listen 50509  # different listen port
    python udp_relay.py --dest 169.254.191.50
"""
import argparse
import socket
import sys
import time

BOARD_IP = "169.254.255.255"   # subnet broadcast: unicast ARP is unreliable on this link
BOARD_PORT = 50507          # the firmware's PORT_CTL
LISTEN_PORT = 50508


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=LISTEN_PORT)
    ap.add_argument("--dest", default=BOARD_IP,
                    help="default is the 169.254/16 subnet broadcast; unicast ARP often fails")
    ap.add_argument("--board-port", type=int, default=BOARD_PORT)
    args = ap.parse_args()

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("0.0.0.0", args.listen))

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    print("relay: listen 0.0.0.0:%d  ->  %s:%d"
          % (args.listen, args.dest, args.board_port), flush=True)

    forwarded = 0
    errors = 0
    while True:
        try:
            data, peer = rx.recvfrom(2048)
        except KeyboardInterrupt:
            break
        except OSError as exc:
            print("rx error: %r" % (exc,), flush=True)
            continue
        if not data:
            continue

        payload = data[:1] if len(data) > 1 else data   # control plane is 1 byte
        try:
            tx.sendto(payload, (args.dest, args.board_port))
            forwarded += 1
            print("%s  %-16s %r -> %d ok" % (
                time.strftime("%H:%M:%S"), "%s:%d" % peer,
                payload.decode("ascii", "replace"), forwarded), flush=True)
        except OSError as exc:
            errors += 1
            print("fwd error #%d: %r" % (errors, exc), flush=True)

    rx.close()
    tx.close()
    print("relay done: forwarded=%d errors=%d" % (forwarded, errors), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
