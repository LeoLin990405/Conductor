# Conductor

**マルチエージェント協調の統合オペレーティングプラットフォーム。**

Conductor は Super-agent のディスパッチ基盤です。目標、コードレビューのコメント、定期タスクのどれも同じパイプラインの Ticket になり、PTC エージェントまたはローカルのネイティブ CLI Worker が実行します。スレッド、ファイル、成果物、レポート、そしてあなたの回答が必要な質問は、一つの Inbox に集約されます。完全なドキュメントは [README](../README.md) を参照してください。

- 単一の入口：Inbox、Tasks、Agents、Lines、Agent fleet、Runs（英語 UI のラベルは Logs）、Sources（GitHub、Feishu、cron）、Plugins、Settings
- 統一された Ticket パイプライン：手動タスク、GitHub コメント、cron スケジュールはすべて同じ種類の Ticket になります（待機、実行中、入力待ち、ブロック、失敗、取消、再試行、レポート）
- 複数の実行体：PTC worker に加え、11 種のネイティブ CLI（Codex、Claude Code、Cursor、DeepSeek DSH、Kimi、MiniMax、MiMo、Grok、StepFun、ZCode、GLM）。セッション再開とキャッシュ使用量の報告は CLI ごとに異なります。Gemini と Qwen はネイティブ Worker としては登録されず、PTC のみです
- ローカル daemon：登録した daemon が Ticket を取得し、OS サンドボックス内で実行します（Linux は `bwrap`、macOS は `sandbox-exec`。Linux のサンドボックスはホストとネットワーク名前空間を共有するため、ループバックポートには到達できます）。クラウド Worker は既定で拒否され、明示的に有効化した Docker ランタイム（`AUTONOMY_CLOUD_WORKER_RUNTIME=docker`）でのみ実行されます
- 並行数の上限：`AUTONOMY_MAX_RUNNING_WORKERS`（既定 4）は、ローカル daemon が Ticket を claim するときと、クラウド Worker を起動するときに確認するグローバルのスロット上限です（起動時に空きスロットがない Ticket は queued のまま残り、後で再試行されます）。daemon は 1–32 件の並行 Ticket を設定でき、既定は 1 です
- 確実な配送：lease、再試行、冪等キーを持つ durable effect / outbox、Postgres advisory lock、stall watchdog
- モデル基盤：モデルマニフェスト、provider ルーティング、fallback、BYOK/OAuth。Super-agent Router は DeepSeek アカウント経由の DeepSeek-V4.1-Flash に固定されています
- ワークスペース、共有ファイル、skills、MCP、Plugins、Settings はアプリシェルに残ります。旧 Chat、Projects、Memory、Automations のページは廃止され、ディープリンクは Inbox に戻ります
- `conductor` CLI（Ticket、Sources、daemon、dispatch、deploy / rollback）と、ホスト型 Web アプリを包む Electron デスクトップシェル

[English](README.en.md) · [中文](README.zh-CN.md)
