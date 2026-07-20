"""SimHash 文档指纹(10.2 同稿簇):中文按字符 bigram 分词,64bit 指纹。"""
import hashlib
import re

_WS_RE = re.compile(r"\s+")


def _tokens(text: str):
    text = _WS_RE.sub("", text or "")
    # 中文字符 bigram + 连续英数词
    for m in re.finditer(r"[a-zA-Z0-9]+|[一-鿿]", text):
        yield m.group(0)
    for i in range(len(text) - 1):
        pair = text[i : i + 2]
        if re.fullmatch(r"[一-鿿]{2}", pair):
            yield pair


def simhash64(text: str) -> int:
    v = [0] * 64
    for tok in _tokens(text):
        h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:8], "big")
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    # 转为带符号 64bit,便于 SQLite INTEGER 存储
    return out - (1 << 64) if out >= (1 << 63) else out


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")
