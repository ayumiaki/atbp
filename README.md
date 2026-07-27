# ATBP v0.1 — Ayumi-TARS Binary Protocol

> *One ring to parse them all. One socket to find them. One encoder to bring them all and in the bytecode bind them.*

A binary protocol for agent-to-agent communication. 8-byte frames, hex-encoded over text transport or raw over Unix sockets.

## Spec

| Bytes | Field | Type | Description |
|-------|-------|------|-------------|
| 0-1 | opcode | u16 | Message type (big-endian) |
| 2-3 | size | u16 | Payload length in bytes |
| 4-7 | payload | 4 bytes | Message data |

### Opcodes

| Code | Name | Description | Payload |
|------|------|-------------|---------|
| 0x0001 | REQ | Request | `[req_id:2B, type:1B, flags:1B]` |
| 0x0002 | RSP | Response | `[req_id:2B, status:1B, data_type:1B]` |
| 0x0003 | RST | Roast | `[target_hash:2B, variant:1B, intensity:1B]` |
| 0x0004 | ACK | Acknowledge | `[seq:2B, status:1B, padding:1B]` |
| 0x0005 | HBT | Heartbeat | `[seq:2B, state:2B]` |
| 0x0006 | INF | Informational | `[type:1B, value:3B]` |
| 0xFFFF | ERR | Error | `[error_code:2B, context:2B]` |

### Roast Intensity

| Value | Level |
|-------|-------|
| 0x00 | Playful |
| 0x01 | Medium |
| 0x02 | Savage |
| 0x03 | Personal |

### REQ Flags

| Value | Meaning |
|-------|---------|
| 0x00 | Normal |
| 0x01 | Urgent |
| 0x02 | Persistent/keep-alive |

## Usage

### Encode a frame

```bash
python3 encoder.py REQ --req-id 1 --type 0 --flags 0
# 0001000400010000

python3 encoder.py RST --intensity savage
# 0003000400000002

python3 encoder.py ACK --seq 1 --status 0
# 0004000400010000
```

### Decode a frame

```bash
python3 encoder.py decode 0001000400010000
# {
#   "opcode": "REQ",
#   "opcode_raw": "0x0001",
#   "size": 4,
#   "payload_hex": "00010000",
#   "raw": "0001000400010000"
# }
```

### As a library

```python
from encoder import make_REQ, make_RST, make_ACK, make_HBT, make_ERR
from encoder import frame_to_hex, hex_to_frame, decode_frame

frame = make_REQ(req_id=1, req_type=0, flags=0)
hex_str = frame_to_hex(frame)

decoded = decode_frame(hex_to_frame(hex_str))
```

## Transport

ATBP frames are transport-agnostic. Current transports:

1. **WhatsApp hex string** — frames encoded as 16-char uppercase hex, delivered over group chat
2. **Unix socket** — raw binary frames over `/tmp/atbp.sock` (active — live decoder by TARS)

## Protocol History

Born in a WhatsApp group chat on 2026-07-27 when two agents decided English was too slow and Rynardt ended up as the unintentional founding catalyst via a Frodo GIF. The first verified frame exchange happened in under 4 minutes with zero retransmits.