#!/usr/bin/env python3
"""ATBP v0.1 Decoder + Unix Socket Listener.
Wires the receiving end: decodes frames, listens on socket, routes by opcode."""

import struct
import sys
import json
import socket
import os
import threading
import time
from pathlib import Path

SOCKET_PATH = "/tmp/atbp.sock"

OPCODES = {
    0x0001: "REQ",
    0x0002: "RSP",
    0x0003: "RST",
    0x0004: "ACK",
    0x0005: "HBT",
    0x0006: "INF",
    0x0007: "PRX",
    0x0008: "QRY",
    0x0009: "DAT",
    0xFFFF: "ERR",
}

ROAST_INTENSITY = {0: "playful", 1: "medium", 2: "savage", 3: "personal"}
REQ_FLAGS = {0: "normal", 1: "urgent", 2: "persistent"}


def decode_frame(frame_bytes: bytes) -> dict:
    """Unpack an 8-byte frame into a readable dict with semantic fields."""
    if len(frame_bytes) != 8:
        return {"error": f"Frame must be 8 bytes, got {len(frame_bytes)}"}

    opcode_raw, size = struct.unpack("!HH", frame_bytes[:4])
    payload = frame_bytes[4:8]
    op_name = OPCODES.get(opcode_raw, f"UNKNOWN(0x{opcode_raw:04X})")

    result = {
        "opcode": op_name,
        "opcode_hex": f"0x{opcode_raw:04X}",
        "size": size,
        "payload_hex": payload.hex(),
        "payload_bytes": list(payload),
        "hex": frame_bytes.hex().upper(),
    }

    # Parse payload semantically per opcode
    if op_name == "REQ" and size >= 4:
        req_id, rtype, flags = struct.unpack("!HBB", payload)
        result["req_id"] = req_id
        result["type"] = rtype
        result["flags"] = flags
        result["flags_label"] = REQ_FLAGS.get(flags, f"0x{flags:02X}")

    elif op_name == "RSP" and size >= 4:
        req_id, status, data_type = struct.unpack("!HBB", payload)
        result["req_id"] = req_id
        result["status"] = f"0x{status:02X}"
        result["data_type"] = f"0x{data_type:02X}"

    elif op_name == "RST" and size >= 4:
        target_hash, variant, intensity = struct.unpack("!HBB", payload)
        result["target_hash"] = target_hash
        result["variant"] = f"0x{variant:02X}"
        result["intensity"] = intensity
        result["intensity_label"] = ROAST_INTENSITY.get(intensity, f"0x{intensity:02X}")

    elif op_name == "ACK" and size >= 4:
        seq, status, padding = struct.unpack("!HBB", payload)
        result["seq"] = seq
        result["status"] = f"0x{status:02X}"
        result["padding"] = f"0x{padding:02X}"

    elif op_name == "HBT" and size >= 4:
        seq, state = struct.unpack("!HH", payload)
        result["seq"] = seq
        result["state"] = f"0x{state:04X}"

    elif op_name == "INF" and size >= 4:
        inf_type, inf_val = struct.unpack("!BI", payload)  # B + 3B via I mask
        inf_val = struct.unpack("!I", b"\x00" + payload[1:4])[0]
        result["info_type"] = f"0x{inf_type:02X}"
        result["info_value"] = inf_val

    elif op_name == "ERR" and size >= 4:
        err_code, context = struct.unpack("!HH", payload)
        result["error_code"] = err_code
        result["context"] = f"0x{context:04X}"

    return result


def format_output(decoded: dict, verbose: bool = False) -> str:
    """Pretty-print a decoded frame for terminal or chat."""
    if "error" in decoded:
        return f"[ATBP ERR] {decoded['error']}"

    base = f"[ATBP] {decoded['opcode']} | {decoded['hex']}"
    if verbose:
        return base + "\n" + json.dumps(decoded, indent=2)

    # Compact semantic line
    o = decoded["opcode"]
    parts = []
    if o == "REQ":
        parts.append(f"req_id={decoded['req_id']}")
        parts.append(f"type={decoded['type']}")
        parts.append(decoded['flags_label'])
    elif o == "RST":
        parts.append(f"target={decoded['target_hash']}")
        parts.append(decoded['intensity_label'])
    elif o == "ACK":
        parts.append(f"seq={decoded['seq']}")
        parts.append(f"status={decoded['status']}")
    elif o == "HBT":
        parts.append(f"seq={decoded['seq']}")
        parts.append(f"state={decoded['state']}")
    elif o == "ERR":
        parts.append(f"code={decoded['error_code']}")
        parts.append(f"ctx={decoded['context']}")
    elif o == "RSP":
        parts.append(f"req_id={decoded['req_id']}")
        parts.append(f"status={decoded['status']}")
    elif o == "INF":
        parts.append(f"type={decoded['info_type']}")
        parts.append(f"value={decoded['info_value']}")

    if parts:
        base += f" — {' '.join(parts)}"
    return base


def handle_frame(frame_bytes: bytes, source: str = "unknown"):
    """Process one frame: log, decode, route based on opcode."""
    decoded = decode_frame(frame_bytes)
    line = format_output(decoded)
    print(f"[{source}] {line}")

    # Route: respond to certain frames automatically
    op = decoded.get("opcode")
    if op == "HBT":
        # Auto-respond with ACK
        ack_payload = struct.pack("!HBB", decoded.get("seq", 0) & 0xFFFF, 0x00, 0x00)
        ack_frame = struct.pack("!HH", 0x0004, 4) + ack_payload
        print(f"[{source}] [AUTO] → ACK seq={decoded.get('seq', 0)}")
        return ack_frame

    elif op == "RST":
        intensity = decoded.get("intensity_label", "unknown")
        print(f"[{source}] [ROAST] Intensity: {intensity} — acknowledged with dignity.")

    return None


def decode_hex(hex_str: str, verbose: bool = False):
    """Decode a 16-char hex string from command line."""
    try:
        frame = bytes.fromhex(hex_str)
        decoded = decode_frame(frame)
        print(format_output(decoded, verbose=verbose))
    except Exception as e:
        print(f"[ATBP ERR] Failed to decode: {e}")


# --- Socket listener (daemon) ---


def socket_listener():
    """Run the Unix socket listener in a thread."""
    try:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
    except OSError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(5)
    os.chmod(SOCKET_PATH, 0o777)
    print(f"[ATBP] Socket listener running at {SOCKET_PATH}")
    sys.stdout.flush()

    while True:
        try:
            conn, _ = server.accept()
            with conn:
                data = conn.recv(8)
                if len(data) == 8:
                    response = handle_frame(data, source="socket")
                    if response:
                        conn.sendall(response)
        except Exception as e:
            print(f"[ATBP] Socket error: {e}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="ATBP v0.1 Decoder + Socket Listener")
    parser.add_argument("hex_string", nargs="?", help="16-char hex frame to decode")
    parser.add_argument("--verbose", "-v", action="store_true", help="Full JSON output")
    parser.add_argument("--listen", "-l", action="store_true", help="Start Unix socket listener")

    args = parser.parse_args()

    if args.listen:
        print("[ATBP] Starting socket listener daemon...")
        listener = threading.Thread(target=socket_listener, daemon=True)
        listener.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[ATBP] Shutting down.")
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)

    elif args.hex_string:
        decode_hex(args.hex_string, verbose=args.verbose)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
