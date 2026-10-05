"""パーサと共通型のテスト。"""
from __future__ import annotations

from datetime import date

from jarvis.models import SourceResult, failure, redact
from jarvis.parsing import parse_date, parse_minutes, parse_yen


def test_parse_yen_keeps_blank_as_none() -> None:
    assert parse_yen("¥7,320,546") == 7320546
    assert parse_yen("-¥638,640") == -638640
    assert parse_yen("△1,000") == -1000
    assert parse_yen("") is None  # 空欄を0にしない
    assert parse_yen("未入力") is None


def test_parse_date_variants() -> None:
    assert parse_date("2026/08/04（仮）") == date(2026, 8, 4)
    assert parse_date("2026-09-09") == date(2026, 9, 9)
    assert parse_date("2026/07/31以降") == date(2026, 7, 31)
    assert parse_date("未定") is None
    assert parse_date("2026/02/30") is None


def test_parse_minutes() -> None:
    assert parse_minutes("30分") == 30
    assert parse_minutes("2〜3時間") == 180  # 上限側
    assert parse_minutes("1時間+30分") == 90
    assert parse_minutes("複数回") is None
    assert parse_minutes("") is None


def test_redact_hides_tokens() -> None:
    msg = "GET https://graph.facebook.com/v21.0/act_1?access_token=EAABsecret123&x=1 xoxb-123-abc Bearer abc.def"
    out = redact(msg)
    assert "EAABsecret123" not in out and "xoxb-123-abc" not in out and "abc.def" not in out
    assert "access_token=***" in out


def test_failure_has_no_data() -> None:
    r = failure("cash", "資金", RuntimeError("boom token=abc"))
    assert r.status == "error" and r.data == {} and not r.usable
    assert "abc" not in r.errors[0]


def test_source_result_roundtrip() -> None:
    r = SourceResult(name="x", label="X", status="ok", data={"a": 1})
    assert SourceResult.from_dict(r.to_dict()) == r
