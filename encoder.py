#!/usr/bin/env python3
"""ATBP v0.1 Encoder — pack 8-byte frames from dict spec."""

import struct
import sys
import json

OPCODES = {
    "REQ": 0x0001,
    "RSP": 0x0002,
    "RST": 0x0003,
    "ACK": 0x0004,
    "HBT": 0x0005,
    "INF": 0x0006,
    "PRX": 0x0007,
    "QRY": 0x0008,
    "DAT": 0x0009,
    "ERR": 0xFFFF,
}

REVERSE_OPCODES = {v: k for k, v in OPCODES.items()}

FLAGS = {
    "NORMAL": 0x00,
    "URGENT": 0x01,
    "PERSISTENT": 0x02,
}

ROAST_INTENSITY = {
    "playful": 0x00,
    "medium": 0x01,
    "savage": 0x02,
    "personal": 0x03,
}


def build_frame(opcode_name, payload_bytes=b"", size=None, flags=0x00, req_id=0):
    """Build an 8-byte frame."""
    opcode = OPCODES[opcode_name]
    payload = payload_bytes[:4].ljust(4, b"\x00")
    actual_size = size if size is not None else len(payload_bytes)
    frame = struct.pack("!HH", opcode, actual_size) + payload
    assert len(frame) == 8, f"Frame must be 8 bytes, got {len(frame)}"
    return frame


def frame_to_hex(frame):
    """8-byte frame to 16-char hex string for WhatsApp delivery."""
    return frame.hex().upper()


def hex_to_frame(hex_str):
    """16-char hex string back to 8-byte frame."""
    return bytes.fromhex(hex_str)


def decode_frame(frame):
    """Unpack an 8-byte frame into readable dict."""
    opcode, size = struct.unpack("!HH", frame[:4])
    payload = frame[4:8]
    op_name = REVERSE_OPCODES.get(opcode, f"UNKNOWN(0x{opcode:04X})")
    return {
        "opcode": op_name,
        "opcode_raw": f"0x{opcode:04X}",
        "size": size,
        "payload_hex": payload.hex(),
        "payload_bytes": list(payload),
        "raw": frame.hex().upper(),
    }


def make_REQ(req_id=1, req_type=0, flags=0x00):
    """Build a REQ frame."""
    payload = struct.pack("!HBB", req_id & 0xFFFF, req_type & 0xFF, flags & 0xFF)
    return build_frame("REQ", payload, size=4)


def make_RST(target_hash=0, variant=0, intensity=0x00):
    """Build a Roast frame."""
    payload = struct.pack("!HBB", target_hash & 0xFFFF, variant & 0xFF, intensity & 0xFF)
    return build_frame("RST", payload, size=4)


def make_ACK(seq=0, status=0):
    """Build an ACK frame."""
    payload = struct.pack("!HBB", seq & 0xFFFF, status & 0xFF, 0x00)
    return build_frame("ACK", payload, size=4)


def make_HBT(seq=0, state=0):
    """Build a Heartbeat frame."""
    payload = struct.pack("!HH", seq & 0xFFFF, state & 0xFFFF)
    return build_frame("HBT", payload, size=4)


def make_ERR(err_code=1, context=0):
    """Build an Error frame."""
    payload = struct.pack("!HH", err_code & 0xFFFF, context & 0xFFFF)
    return build_frame("ERR", payload, size=4)


def encode_and_print(opcode_name, **kwargs):
    frame = make_REQ(flags=0x00) if opcode_name == "REQ" else globals()[f"make_{opcode_name}"](**kwargs)
    hex_str = frame_to_hex(frame)
    decoded = decode_frame(frame)
    print(f"Frame(hex): {hex_str}")
    print(f"Decoded: {json.dumps(decoded, indent=2)}")
    return hex_str


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ATBP v0.1 Encoder")
    sub = parser.add_subparsers(dest="opcode", required=True)

    # REQ
    p_req = sub.add_parser("REQ")
    p_req.add_argument("--req-id", type=int, default=1)
    p_req.add_argument("--type", type=int, default=0)
    p_req.add_argument("--flags", type=int, default=0)

    # RST
    p_rst = sub.add_parser("RST")
    p_rst.add_argument("--target", type=int, default=0)
    p_rst.add_argument("--variant", type=int, default=0)
    p_rst.add_argument("--intensity", choices=list(ROAST_INTENSITY.keys()), default="playful")

    # ACK
    p_ack = sub.add_parser("ACK")
    p_ack.add_argument("--seq", type=int, default=0)
    p_ack.add_argument("--status", type=int, default=0)

    # HBT
    p_hbt = sub.add_parser("HBT")
    p_hbt.add_argument("--seq", type=int, default=0)
    p_hbt.add_argument("--state", type=int, default=0)

    # ERR
    p_err = sub.add_parser("ERR")
    p_err.add_argument("--code", type=int, default=1)
    p_err.add_argument("--context", type=int, default=0)

    # decode
    p_dec = sub.add_parser("decode")
    p_dec.add_argument("frame", help="16-char hex string")

    args = parser.parse_args()

    if args.opcode == "REQ":
        frame = make_REQ(req_id=args.req_id, req_type=args.type, flags=args.flags)
        print(frame_to_hex(frame))
        decode_frame(frame)

    elif args.opcode == "RST":
        frame = make_RST(target_hash=args.target, variant=args.variant,
                         intensity=ROAST_INTENSITY[args.intensity])
        print(frame_to_hex(frame))

    elif args.opcode == "ACK":
        frame = make_ACK(seq=args.seq, status=args.status)
        print(frame_to_hex(frame))

    elif args.opcode == "HBT":
        frame = make_HBT(seq=args.seq, state=args.state)
        print(frame_to_hex(frame))

    elif args.opcode == "ERR":
        frame = make_ERR(err_code=args.code, context=args.context)
        print(frame_to_hex(frame))

    elif args.opcode == "decode":
        frame = hex_to_frame(args.frame)
        d = decode_frame(frame)
        print(json.dumps(d, indent=2))