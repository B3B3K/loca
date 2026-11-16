"""Checksum brute-forcer for reverse-engineered frames."""
ALGOS = {  # name: (width, poly, init, refin, refout, xorout)
    "crc8": (8, 0x07, 0x00, False, False, 0x00),
    "crc8-maxim": (8, 0x31, 0x00, True, True, 0x00),
    "crc8-itu": (8, 0x07, 0x00, False, False, 0x55),
    "crc16-modbus": (16, 0x8005, 0xFFFF, True, True, 0x0000),
    "crc16-ccitt-false": (16, 0x1021, 0xFFFF, False, False, 0x0000),
    "crc16-xmodem": (16, 0x1021, 0x0000, False, False, 0x0000),
    "crc16-kermit": (16, 0x1021, 0x0000, True, True, 0x0000),
    "crc16-arc": (16, 0x8005, 0x0000, True, True, 0x0000),
    "crc32": (32, 0x04C11DB7, 0xFFFFFFFF, True, True, 0xFFFFFFFF),
}


def _rev(v, bits):
    return int(f"{v:0{bits}b}"[::-1], 2)


def crc_generic(data, width, poly, init, refin, refout, xorout):
    top, mask, reg = 1 << (width - 1), (1 << width) - 1, init
    for b in data:
        if refin:
            b = _rev(b, 8)
        reg ^= b << (width - 8)
        for _ in range(8):
            reg = ((reg << 1) ^ poly) if reg & top else reg << 1
            reg &= mask
    if refout:
        reg = _rev(reg, width)
    return reg ^ xorout


def compute_all(data):
    out = {n: crc_generic(data, *p) for n, p in ALGOS.items()}
    out["sum8"] = sum(data) & 0xFF
    out["xor8"] = _xor(data)
    out["neg-sum8"] = (-sum(data)) & 0xFF
    return out


def _xor(data):
    r = 0
    for b in data:
        r ^= b
    return r


def widths():
    return {n: p[0] // 8 for n, p in ALGOS.items()} | {"sum8": 1, "xor8": 1, "neg-sum8": 1}


def brute(frame, expect=None):
    """Return list of matches (algo, start, end, endian) or the plain table when expect is None."""
    if expect is None:
        return [f"{n}: {v:#x}" for n, v in compute_all(frame).items()]
    exp = bytes.fromhex(expect)
    w, hits = len(exp), []
    for i in range(len(frame)):
        for j in range(i + 1, len(frame) + 1):
            for n, v in compute_all(frame[i:j]).items():
                if widths()[n] != w:
                    continue
                for endian in ("big", "little"):
                    if v.to_bytes(w, endian) == exp:
                        hits.append(f"{n} bytes[{i}:{j}] {endian}-endian")
                if w == 1:
                    break
    return hits
