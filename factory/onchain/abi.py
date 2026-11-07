"""チェーンの数字と文字の読み書き(16進・32バイトの語・ログの種類・関数の番号)。"""
from __future__ import annotations

from .keccak import keccak256

WORD = 64   # 32バイト = 16進で64文字


def topic(signature: str) -> str:
    """ログの種類。例: topic("Transfer(address,address,uint256)") → 0xddf252ad…"""
    return "0x" + keccak256(signature.encode()).hex()


def selector(signature: str) -> str:
    """関数の番号(先頭4バイト)。例: selector("transfer(address,uint256)") → 0xa9059cbb"""
    return "0x" + keccak256(signature.encode()).hex()[:8]


def to_int(value: str | int | None) -> int:
    """チェーンの答えの数字("0x1a" など)を整数に。"""
    if value is None:
        raise ValueError("数字がない")
    if isinstance(value, int):
        return value
    return int(value, 16) if value.startswith(("0x", "0X")) else int(value)


def address(value: str) -> str:
    """アドレスを小文字の 0x+40文字にそろえる。32バイトの語(ログの topic など)なら末尾の20バイトを取る。"""
    h = value[2:] if value.startswith(("0x", "0X")) else value
    if len(h) not in (40, WORD):
        raise ValueError(f"アドレスではない: {value}")
    return "0x" + h[-40:].lower()


def words(data: str) -> list[int]:
    """ログの data や eth_call の答えを、32バイトごとの整数に分ける。"""
    h = data[2:] if data.startswith(("0x", "0X")) else data
    if len(h) % WORD:
        raise ValueError(f"32バイトの区切りになっていない({len(h) // 2}バイト)")
    return [int(h[i:i + WORD], 16) for i in range(0, len(h), WORD)]


def encode_call(signature: str, *args: int | str | bool) -> str:
    """固定長の引数(uint・address・bool)だけの関数の呼び出しを作る(eth_call の data)。"""
    out = [selector(signature)[2:]]
    for a in args:
        if isinstance(a, bool):
            v = int(a)
        elif isinstance(a, int):
            if a < 0:
                raise ValueError("負の数は扱わない")
            v = a
        else:
            v = int(address(a), 16)
        out.append(f"{v:064x}")
    return "0x" + "".join(out)


def decode_addresses(data: str) -> list[str]:
    """address[] の答え(getAllMarkets() など)を住所の並びに。"""
    w = words(data)
    if not w:
        return []
    off = w[0] // 32
    n = w[off] if len(w) > off else 0
    return [address(f"0x{x:064x}") for x in w[off + 1:off + 1 + n]]


def decode_string(data: str) -> str:
    """文字の答え(symbol() など)を読む。古い仕組みの32バイト固定の形にも合わせる。"""
    h = data[2:] if data.startswith(("0x", "0X")) else data
    raw = bytes.fromhex(h)
    if len(raw) == 32:
        return raw.rstrip(b"\x00").decode("utf-8", "replace")
    if len(raw) < 64:
        raise ValueError("文字の答えではない")
    offset = int.from_bytes(raw[:32], "big")
    length = int.from_bytes(raw[offset:offset + 32], "big")
    return raw[offset + 32:offset + 32 + length].decode("utf-8", "replace")


def encode_aggregate3(calls: list[tuple[str, str]], allow_failure: bool = True) -> str:
    """Multicall3 の aggregate3((address,bool,bytes)[]) の data。calls は (宛先, data) の並び。
    1回の eth_call で多くの読み取りをまとめるのに使う。"""
    tuples = []
    for target, data in calls:
        body = data.removeprefix("0x")
        n = len(body) // 2
        tuples.append(f"{int(address(target), 16):064x}" + f"{int(allow_failure):064x}" + f"{96:064x}"
                      + f"{n:064x}" + body.ljust(-(-len(body) // WORD) * WORD, "0"))
    offsets, pos = [], 32 * len(tuples)
    for t in tuples:
        offsets.append(f"{pos:064x}")
        pos += len(t) // 2
    return (selector("aggregate3((address,bool,bytes)[])") + f"{32:064x}" + f"{len(tuples):064x}"
            + "".join(offsets) + "".join(tuples))


def decode_aggregate3(data: str) -> list[tuple[bool, str]]:
    """aggregate3 の答え((bool,bytes)[])を (通ったか, 答えの data) の並びに。"""
    raw = bytes.fromhex(data.removeprefix("0x"))

    def word_at(i: int) -> int:
        return int.from_bytes(raw[i:i + 32], "big")

    arr = word_at(0)
    n = word_at(arr)
    base = arr + 32
    out = []
    for k in range(n):
        t = base + word_at(base + 32 * k)
        ok = bool(word_at(t))
        b = t + word_at(t + 32)
        length = word_at(b)
        out.append((ok, "0x" + raw[b + 32:b + 32 + length].hex()))
    return out
