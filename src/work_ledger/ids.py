"""RFC 9562 UUIDv7 generation and helpers.

Pure stdlib. No I/O, no imports from any other work_ledger module.
"""
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = -1
_last_rand_a = 0


def uuid7(when_ms: int | None = None) -> str:
    """Generate an RFC 9562 UUIDv7 string (lowercase, hyphenated).

    Layout (16 bytes / 128 bits):
      - 48 bits: big-endian Unix timestamp in milliseconds
      - 4 bits:  version (0x7)
      - 12 bits: rand_a (monotonic counter within a millisecond, else random)
      - 2 bits:  variant (0b10)
      - 62 bits: rand_b (random)

    When when_ms is explicitly provided, monotonicity is guaranteed only via
    the timestamp ordering (callers supplying increasing when_ms get
    increasing ids). When when_ms is None (the common case), a module-level
    (last_ms, last_seq) is used to guarantee monotonic ordering even for
    multiple ids generated within the same millisecond.
    """
    global _last_ms, _last_rand_a

    explicit = when_ms is not None
    ms = when_ms if explicit else int(time.time() * 1000)

    if explicit:
        rand_a = uuid.uuid4().int & 0xFFF
    else:
        with _lock:
            if ms <= _last_ms:
                ms = _last_ms
                _last_rand_a = (_last_rand_a + 1) & 0xFFF
                if _last_rand_a == 0:
                    # 12-bit counter wrapped within the same millisecond;
                    # bump the timestamp to preserve monotonicity.
                    ms += 1
                    _last_ms = ms
            else:
                _last_rand_a = uuid.uuid4().int & 0xFFF
                _last_ms = ms
            rand_a = _last_rand_a

    rand_b = uuid.uuid4().int & ((1 << 62) - 1)

    b = bytearray(16)
    b[0:6] = ms.to_bytes(6, "big")
    b[6] = 0x70 | ((rand_a >> 8) & 0x0F)
    b[7] = rand_a & 0xFF
    b[8] = 0x80 | ((rand_b >> 56) & 0x3F)
    b[9:16] = (rand_b & ((1 << 56) - 1)).to_bytes(7, "big")

    return str(uuid.UUID(bytes=bytes(b)))


def new_id() -> str:
    """Alias for uuid7()."""
    return uuid7()


def timestamp_ms_of(uuid7_str: str) -> int:
    """Extract the 48-bit millisecond timestamp embedded in a UUIDv7 string."""
    u = uuid.UUID(uuid7_str)
    return u.int >> 80
