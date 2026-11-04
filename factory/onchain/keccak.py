"""Keccak-256(イーサリアムの方式)。標準ライブラリの sha3_256 は詰め方が違い、使えない。

ログの種類(topic)や関数の番号(selector)を求めるのに使う。速さは要らないので Python だけで書く。
"""
from __future__ import annotations

_RATE = 136          # 1088ビット
_MASK = (1 << 64) - 1
_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
# 回す量 r[x][y]
_ROT = ((0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61), (28, 55, 25, 21, 56),
        (27, 20, 39, 8, 14))


def _rol(v: int, n: int) -> int:
    return ((v << n) | (v >> (64 - n))) & _MASK if n else v


def _permute(a: list[int]) -> None:
    for rc in _RC:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for i in range(25):
            a[i] ^= d[i % 5]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _ROT[x][y])
        for y in range(5):
            row = b[5 * y:5 * y + 5]
            for x in range(5):
                a[x + 5 * y] = row[x] ^ (~row[(x + 1) % 5] & _MASK & row[(x + 2) % 5])
        a[0] ^= rc


def keccak256(data: bytes) -> bytes:
    padded = bytearray(data)
    padded.append(0x01)
    padded.extend(b"\x00" * (-len(padded) % _RATE))
    padded[-1] |= 0x80
    state = [0] * 25
    for off in range(0, len(padded), _RATE):
        block = padded[off:off + _RATE]
        for i in range(_RATE // 8):
            state[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        _permute(state)
    return b"".join(state[i].to_bytes(8, "little") for i in range(4))
