"""Slack 取得のテスト（偽APIで差分取得・カバレッジ・重複統合を検証）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from jarvis.config import JST
from jarvis.sources import slack

OWNER = "U_OWNER"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=JST)


def ts(day: int, h: int = 10) -> str:
    return f"{datetime(2026, 10, day, h, 0, tzinfo=JST).timestamp():.6f}"


class FakeResp:
    def __init__(self, body: dict, status: int = 200, headers: dict | None = None) -> None:
        self._b, self.status_code, self.headers = body, status, headers or {}

    def json(self) -> dict:
        return self._b


class FakeSlack:
    """auth.test / conversations.* だけ応答する偽Slack。呼び出しを記録する。"""

    def __init__(self, *, scopes: str, user_token: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.scopes = scopes
        self.user_token = user_token
        self.history: dict[str, list[dict]] = {
            "C1": [{"ts": ts(3), "user": "U2", "text": f"<@{OWNER}> 見積の確認をお願いします。10/7までに", "reply_count": 1, "latest_reply": ts(3, 11)},
                   {"ts": ts(4), "user": "U3", "text": "ランチ行きましょう"}],
            "C2": [{"ts": ts(4, 9), "user": "U4", "text": f"<@{OWNER}> 見積の確認をお願いします。10/7までに！"}],
        }
        self.replies = {ts(3): [{"ts": ts(3), "user": "U2", "text": self.history["C1"][0]["text"], "reply_count": 1, "latest_reply": ts(3, 11)},
                                {"ts": ts(3, 11), "user": OWNER, "text": "確認します。金額はいくらですか？"}]}
        self.reply_calls = 0

    def __call__(self, url: str, params: dict, headers: dict, timeout: int) -> FakeResp:
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, dict(params)))
        h = {"x-oauth-scopes": self.scopes}
        if method == "auth.test":
            body = {"ok": True, "url": "https://growth-and-technology.slack.com/", "user_id": OWNER}
            if not self.user_token:
                body["bot_id"] = "B1"
            return FakeResp(body, headers=h)
        if method == "conversations.list":
            if "channels:read" not in self.scopes:
                return FakeResp({"ok": False, "error": "missing_scope"}, headers=h)
            return FakeResp({"ok": True, "channels": [
                {"id": "C1", "name": "06-01_finance_共通", "is_member": True},
                {"id": "C2", "name": "sales", "is_member": False, "is_private": True},
                {"id": "C3", "name": "secret", "is_member": False}]}, headers=h)
        if method == "conversations.history":
            if params["channel"] == "C3":
                return FakeResp({"ok": False, "error": "not_in_channel"}, headers=h)
            msgs = [m for m in self.history.get(params["channel"], []) if float(m["ts"]) > float(params["oldest"])]
            return FakeResp({"ok": True, "messages": msgs, "has_more": False}, headers=h)
        if method == "conversations.replies":
            self.reply_calls += 1
            return FakeResp({"ok": True, "messages": self.replies.get(params["ts"], [])}, headers=h)
        raise AssertionError(method)


ALL = "channels:read,groups:read,channels:history,groups:history"


def test_classify_and_status_rules() -> None:
    assert "request" in slack.classify("ご確認お願いします")
    assert "failure" in slack.classify("取引先自動分類(R3)：failure")
    root = {"ts": "1", "user": "U2", "text": f"<@{OWNER}> 対応お願いします"}
    assert slack.thread_status(root, [], OWNER) == "未対応"
    assert slack.thread_status(root, [{"ts": "2", "user": OWNER, "text": "確認します"}], OWNER) == "相手待ち"
    assert slack.thread_status(root, [{"ts": "2", "user": "U2", "text": "対応しました"}], OWNER) == "対応済み候補"


def test_readonly_client_rejects_write_methods() -> None:
    api = slack.SlackApi("xoxp-test", http_get=FakeSlack(scopes=ALL))
    with pytest.raises(PermissionError):
        api.call("chat.postMessage", channel="C1", text="x")


def test_full_sync_coverage_dedupe_and_incremental(settings: Any) -> None:
    fake = FakeSlack(scopes=ALL)
    api = slack.SlackApi("xoxp-test", http_get=fake, sleep=lambda s: None)
    res = slack.fetch(settings, api=api, now=NOW)
    d = res.data
    cov = {c["id"]: c["status"] for c in d["coverage"]}
    assert cov == {"C1": "取得可", "C2": "取得可", "C3": "未参加"}
    assert res.status == "partial" and d["channelsReadable"] == 2
    # 2チャンネルの同じ依頼は1件に統合し、元リンクを両方残す
    reqs = [i for i in d["items"] if "見積" in i["text"]]
    assert len(reqs) == 1 and len(reqs[0]["links"]) == 2 and reqs[0]["duplicates"] == 1
    assert all(i["confirmed"] is False for i in d["items"])
    assert all("ランチ" not in i["text"] for i in d["items"])
    assert reqs[0]["link"].startswith("https://growth-and-technology.slack.com/archives/")
    # 差分取得: 2回目は latest_ts 以降だけを読む（全履歴を読み直さない）
    first_calls = len(fake.calls)
    res2 = slack.fetch(settings, api=api, now=NOW)
    hist = [p for m, p in fake.calls[first_calls:] if m == "conversations.history" and p["channel"] == "C1"]
    assert hist and float(hist[0]["oldest"]) == float(ts(4))
    assert res2.data["fetchedMessages"] == 0


def test_bot_token_without_list_scope_is_reported(settings: Any) -> None:
    fake = FakeSlack(scopes="chat:write,files:read", user_token=False)
    res = slack.fetch(settings, api=slack.SlackApi("xoxb-x", http_get=fake), now=NOW)
    assert res.status == "error"
    assert any("不足スコープ" in e for e in res.errors)
    assert any("channels:read" in e for e in res.errors)


def test_unconfigured_without_token(settings: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("JARVIS_SLACK_TOKEN", raising=False)
    res = slack.fetch(settings)
    assert res.status == "unconfigured" and res.data == {}
    assert any("channels:read" in n for n in res.notes)
