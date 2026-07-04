# Session Handoff
更新: 2026-07-04 夕 JST

## 現在の姿
gpucallは「事業」を降り、**菅野個人の主権計算基盤**として運用中。
mainがcanonical（デフォルトブランチ、CI green、AGPL-3.0/SDK Apache-2.0公開）。最新リリース v2.0.71（netcup2デプロイ済み）。

## 2026-07-04セッションの成果（詳細: tasks/sovereign-upgrade-20260704.md）
1. **Qwen3三層**: h200-qwen3-32b / h200-qwen3-30b-a3b / h200x4-qwen3-235b-a22b-fp8 全てbillable検証PASS。rank standardは`frontier_reasoning` capability経由でQwen3-32Bに本番昇格（初本番成功 16:56）。
2. **sovereignty**: `gpucall sovereignty report/reap/reclaim-artifact` 新設。初回実回収122件（全absent検証・レシート）。週次timer常設（netcup2, 日曜03:17）。
3. **artifact回収機構**: worker暗号輸出の対称復号+purge+bucket allowlist。workerのKDF salt修正（plan_idフォールバック除去）。**実LoRA学習エンジンは未実装**（provenance bundleのみ）。
4. **vision並列**: gateway admission 4/6/8（`~/bin/gpucall-gateway-start.sh`固定）+ caller `vision_semaphore: 3`（トップレベル! gemini:配下は読まれない）。6/6 PASS実証。

## 次スレッドでやるべきこと
1. **明朝runの確認**: `ssh macmini 'grep "OverseasVision] 完了" /Users/admin/Developer/news-system/logs/orchestrator_$(date +%Y-%m-%d).log | tail -1'` — 0紙でなければP0完全解決。0紙なら再調査。
2. 実LoRA学習エンジン（peft/trl入りModal image、既存export path接続）— tasks/sovereign-upgrade-20260704.md末尾参照。
3. Panopticonにworker実contextとカタログ宣言の突合probe追加（H100サイジング事故の事前検出）。
4. 市場公開の判断待ち: Show HN等の一回告知は前回推奨のまま未実施（あんたの判断）。

## 運用ノート（重要）
- gateway再起動は**必ず** `ssh netcup2 '~/bin/gpucall-gateway-start.sh'`（admission limits維持）。
- tuple-smoke: H200系cold start 6-10分/H200x4は10-20分。--poll-timeout-seconds 1100-1450、budget: 32B系$2.5 / 235B$8。
- config変更→config hash変化→全route validation無効化→watch自動再検証（1件/600s）か手動tuple-smoke。
- tasks/*.mdは`git add -f`。heredoc+ssh+f-string引用符は壊れる（%書式を使え）。
- 235B旗艦routeはrank standardのfallback層。単発で使うときはtuple-smoke or requested_tuple（管理者権限）で。
