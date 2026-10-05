"""JARVIS の共通データ型。

取得層（sources/）は必ず :class:`SourceResult` を返す。判断層（engine.py）は
SourceResult の ``data`` だけを読み、取得失敗を「0件・0円」として扱わない。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from .config import JST

#: ok=完全取得 / partial=一部失敗 / stale=旧データ表示 / error=取得失敗 /
#: unconfigured=認証・権限が未設定
SourceStatus = Literal["ok", "partial", "stale", "error", "unconfigured"]


def now_jst() -> datetime:
    """現在時刻（日本時間）。"""
    return datetime.now(JST)


def iso(dt: datetime | None) -> str | None:
    """datetime を ISO8601 文字列にする（None はそのまま）。"""
    return dt.isoformat(timespec="seconds") if dt else None


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"xox[abposr]-[A-Za-z0-9-]+"),
    re.compile(r"(access_token|client_secret|refresh_token|token|key)=([^&\s\"']+)", re.I),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.I),
    re.compile(r"EAA[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def redact(text: str) -> str:
    """エラーメッセージ等から認証情報らしき文字列を伏せる。

    ライブラリの例外にはURL（``access_token=`` 付き）がそのまま入ることがあるため、
    画面・ログ・キャッシュに出す前に必ず通す。
    """
    out = str(text)
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) + "=***") if m.lastindex and m.lastindex >= 2 else "***", out)
    return out[:500]


@dataclass
class SourceResult:
    """1データソースの取得結果。

    Attributes:
        name: ソース識別子（todo / cash / flycka / slack / batch）。
        label: 画面表示名。
        status: 取得状態。
        fetched_at: 今回の取得試行時刻。
        last_success_at: 最後に取得に成功した時刻（旧データ表示時に重要）。
        data_as_of: データ自体の対象日時（例: 銀行明細の最終反映日）。
        scope: 取得範囲の説明（期間・件数・タブ等）。
        errors: 失敗理由（認証情報は伏字化済み）。
        notes: 定義・前提の注記。
        data: 判断層に渡す本体。
    """

    name: str
    label: str
    status: SourceStatus
    fetched_at: str | None = None
    last_success_at: str | None = None
    data_as_of: str | None = None
    scope: str = ""
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON 化できる dict にする。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourceResult":
        """キャッシュ等の dict から復元する。"""
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in keys})

    @property
    def usable(self) -> bool:
        """判断に使えるデータを持つか（stale も表示付きで使う）。"""
        return self.status in ("ok", "partial", "stale") and bool(self.data)


def failure(name: str, label: str, exc: BaseException | str, *,
            status: SourceStatus = "error", scope: str = "") -> SourceResult:
    """取得失敗の SourceResult を作る（データは空。0 とは見なさない）。"""
    msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    return SourceResult(name=name, label=label, status=status,
                        fetched_at=iso(now_jst()), scope=scope, errors=[redact(msg)])
