# JARVIS（読み取り専用 MVP）

代表専用の「今日何をすべき？」ダッシュボード＋チャット。Todo・経営計画・資金繰り・Slack・FLYCKA実績・定期バッチ結果を**読み取り専用**で集め、Top3〜5・理由・最初の5分・委任候補・保留事項を返す。

## 起動

```bash
cd work-manager
python -m jarvis.server            # http://127.0.0.1:8787/
python -m jarvis.server --sync     # 起動前に全ソース取得
```
Windows は `jarvis\start_jarvis.bat` でも起動できる。画面右上「最新データを取得」で再同期する。

## 責務境界と接続

| ソース | 取得元（既存の正本を再利用） | 認証 |
|---|---|---|
| Todo・経営計画 | 事業横断Todoマスター（Sheets） | backoffice の SA 鍵・**readonly スコープ**。SA への閲覧共有が必要 |
| 資金繰り | 資金繰り管理_ver2.0 の backoffice バッチ生成タブ＋13週（保存予測） | 同上 |
| FLYCKA | flycka-d2c の `SheetsClient`・`aggregate_orders`・`MetaAdClient` を import | flycka-d2c/.env（既存） |
| Slack | conversations.list / history / replies（GET のみ） | `jarvis/.env` の `JARVIS_SLACK_TOKEN`（User Token 推奨） |
| 定期バッチ | GitHub Actions のジョブログ | gh CLI の利用者認証 |

財務計算・広告取得は再実装しない（backoffice / flycka-d2c が正本）。JARVIS は書き込み API を呼ばない。

## 判断の原則
- 完了・中止・撤退・不要は除外。相手待ち・保留は別枠
- 更新14日超の Todo は「現状確認（10分）」。古い「今日中/今週」を現在の指示にしない
- 資金は「実残高（銀行明細の反映日まで）」「保存予測（前提つき）」「確認待ちの前提」を分け、安全/不足を断定しない
- Shopify売上と Meta帰属売上は別指標。原価等が無いので利益は「不明」
- 取得失敗は 0 件・0 円にせず、前回成功分を「旧データ」と明示
- Slack 由来は推定。確定 Todo を上書きしない

## 環境変数（任意）
`JARVIS_PORT` / `JARVIS_CACHE_DIR` / `JARVIS_GOOGLE_SA` / `JARVIS_TODO_SNAPSHOT`（SA未共有時に手動取得JSONを旧データ表示）/ `JARVIS_SLACK_TOKEN` / `JARVIS_SLACK_OWNER_ID` / `JARVIS_SLACK_INITIAL_DAYS`（既定14）

## テスト
```bash
python -m pytest jarvis/tests -q
```
