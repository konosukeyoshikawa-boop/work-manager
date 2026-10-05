"""表データの値パーサ（純関数）。

空欄・解釈不能は ``None`` を返し、0 に変換しない。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

_DATE_RE = re.compile(r"(\d{4})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})")
_DATETIME_RE = re.compile(
    r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})[ T](\d{1,2}):(\d{2})")


def norm(value: object) -> str:
    """NFKC 正規化して前後空白を除いた文字列。"""
    return unicodedata.normalize("NFKC", str(value if value is not None else "")).strip()


def parse_date(value: object) -> date | None:
    """``2026/09/09`` ``2026-09-09（仮）`` 等から日付を取り出す。"""
    m = _DATE_RE.search(norm(value))
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def parse_datetime(value: object) -> datetime | None:
    """``2026-10-05 02:52`` 形式から naive datetime を取り出す。"""
    m = _DATETIME_RE.search(norm(value))
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]))
    except ValueError:
        return None


def parse_yen(value: object) -> int | None:
    """``¥7,320,546`` ``-¥638,640`` ``△1,000`` ``(1,000)`` を整数円に。空欄は None。"""
    s = norm(value).replace(",", "").replace("¥", "").replace("円", "").replace(" ", "")
    if not s or s in ("-", "—", "―"):
        return None
    neg = False
    if s.startswith(("△", "▲")):
        neg, s = True, s[1:]
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith("-"):
        neg, s = (not neg), s[1:]
    try:
        v = float(s)
    except ValueError:
        return None
    v = -v if neg else v
    return int(round(v))


def parse_minutes(value: object) -> int | None:
    """``30分`` ``1.5時間`` ``2〜3時間`` ``1時間+30分`` を分に。曖昧なものは None。

    範囲指定は上限側を採用する（過小見積もりを避ける）。
    """
    s = norm(value)
    if not s or re.search(r"複数回|再見積|未定|不明", s):
        return None
    total, found = 0.0, False
    for part in re.split(r"[+＋]", s):
        h = re.search(r"(\d+(?:\.\d+)?)(?:\s*[〜~\-]\s*(\d+(?:\.\d+)?))?\s*時間", part)
        m = re.search(r"(\d+)(?:\s*[〜~\-]\s*(\d+))?\s*分", part)
        if h:
            total += float(h[2] or h[1]) * 60
            found = True
        if m:
            total += float(m[2] or m[1])
            found = True
    return int(total) if found else None
