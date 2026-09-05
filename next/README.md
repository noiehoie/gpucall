# gpucall — 新しい決定論カーネル

依頼・品質条件・観測・予算から実行計画を決め、GPUを借り、結果を保存して回収する。
制御判断にLLMを使わない。旧実装に依存しない独立したPythonプロジェクト。

```bash
cd next
uv sync --locked --extra dev --extra modal
uv run pytest -q
uv run gpucall demo
```

`demo` は一時ディレクトリだけを使うfixture。GPU呼び出しは0件。
`uv build` でwheelを生成できる。CLIのインストールは `uv tool install './next[modal]'`。
旧gpucallのCLIを置き換える場合は `--force` を付ける。

2026-09-05の実行記録は [verification.json](verification.json)。Python 3.11/3.13で
各45件の試験が成功。実GPUでinferは `GPUCALL_OK`、visionは `BLUE` をそれぞれ
検証・通常ジョブの2回取得し、4件とも計画再生とSandbox停止を確認した。

## 実装した契約

| 操作 | 決定・証跡 |
|---|---|
| 選択 | 品質証跡、鮮度、task、主権条件、期限、予算を満たす候補を推定総費用順に並べる |
| 再生 | 依頼・プロファイル・観測を保存。同じカーネル版で計画のハッシュを再現 |
| 予算 | 整数micro-USD、SQLiteの原子的予約、ジョブとアカウントの上限、有限回の再試行 |
| 実行 | 開始意図を先に永続化。不明な開始結果は所有タグで照合し、createを繰り返さない |
| 品質 | 文字数、必須リテラル、JSON Schemaを決定論で検査。最新の失敗証跡が過去の成功に優先 |
| 成果物 | 内容ハッシュ、読み出し時の整合性確認、秘密ファイル権限、サイズ・空き容量制限 |
| 回収 | 所有するSandboxを停止。確認できるまで予約を維持。再起動後も継続 |
| 説明 | 構造化JSON、拒否理由、回収レシート、改変検出用のイベントハッシュ鎖 |

推定費用は供給カタログの実行時間見積もり×CPU・メモリを含む単価＋固定費。
入力長からの動的な性能予測は行わない。品質プロファイルの入力上限と実測した時間を
合わせて設定する。GPU価格の自動取得・供給の自動発見は、この版には含まない。

## APIとCLI

configの既定位置は `$XDG_CONFIG_HOME/gpucall-next/config.json`、stateは
`$XDG_STATE_HOME/gpucall-next`。未設定時はそれぞれ `~/.config`、`~/.local/state`。
`--config` / `--state` で明示できる。状態ディレクトリは所有者専用にする。

```bash
gpucall --config config.json --state state validate-config
gpucall --config config.json --state state estimate request.json
gpucall --config config.json --state state submit request.json
gpucall --config config.json --state state tick
gpucall --config config.json --state state jobs
gpucall --config config.json --state state account
gpucall --config config.json --state state audit
```

`submit` は永続化して返る。実行は `tick` または常駐API内のworkerが進める。
`status JOB` / `result JOB` / `cancel JOB` / `replay JOB` も同じ台帳を使う。
ファイルは `import FILE` で登録し、返されたSHA-256・バイト数・MIME型を依頼に含める。
visionはPNG/JPEGを必須とし、inferへの画像混入は拒否する。

API起動は `serve --host 127.0.0.1 --port PORT`。`GPUCALL_OWNER_TOKEN` に32文字以上の
秘密値が必要。既存listenポートを調べてから起動する。Bearer認証し、loopbackに限定する。

| HTTP | 操作 |
|---|---|
| `GET /v1/schema` / `GET /v1/profiles` | 依頼形式・品質プロファイルの取得（CLIは `schema` / `profiles`） |
| `POST /v1/estimate` | 依頼JSONの見積もり |
| `POST /v1/jobs` | 非同期投入、同じkeyの再送は同じジョブ |
| `GET /v1/jobs` / `GET /v1/jobs/{id}` | 一覧・状態 |
| `POST /v1/jobs/{id}/cancel` | 永続キャンセル |
| `GET /v1/jobs/{id}/result` / `replay` | 成果物・計画照合 |
| `POST /v1/artifacts` / `GET /v1/artifacts/{sha}` | バイナリ登録・取得 |
| `GET /v1/account` / `audit` | 予算台帳・監査鎖検査 |
| `GET /healthz` | プロセス死活、認証不要 |

依頼の必須項目は `key`、`profile`、`instruction`、`deadline_ms`、`budget_microusd`。
時刻はUnixミリ秒、金額は整数micro-USD。API利用者はprovider・GPU・image・品質証跡を
上書きできない。configは所有者が管理する。カタログ変更はAPI再起動で読み直す。

## Modalで実行する

1. `worker/uv.lock` とモデルの完全commit SHAを固定する。
2. `build-modal-image --worker-dir worker --model REPO --revision SHA` で
   workerとモデルを含むイメージを事前構築する。CPUビルド費用はジョブ台帳の対象外。
3. そのimage ID、GPU、region、集計単価、寿命、観測期限をconfigに登録する。
4. 公開した評価入力で `validate-execution request.json --execution ID` を投入し、
   workerを動かす。回収後 `certify JOB` で品質証跡を保存する。
5. 以後の通常投入は同一の実行構成・品質プロファイルの有効な成功証跡を必要とする。

実行経路ではモデルをダウンロードしない。Sandboxに秘密鍵・永続Volume・公開portを
渡さず、外向きネットワークを遮断する。固定image内のworkerは非rootで動く。
実行結果不明の開始は自動再投入しない。回収できない間は `cleaning` と予約を残す。
プロバイダー障害時は新しいstateを作らず、同じ台帳で `tick` を再開する。

`scripts/live_validation.py` は有料の公開入力試験。infer/visionそれぞれについて
検証→証跡登録→通常投入→結果保存→回収→計画再生を行い、stateにJSONレポートを残す。
自動の再課金リトライはしない。実GPU試験はCIに含めない。

## 保証の範囲

`privacy=trusted_provider` は所有者が信頼すると指定した供給に限る。
TEEによる秘密保持を要求する `attested` は未対応として拒否する。
`provider_terminated` はSandbox停止の確認であり、プロバイダー内部の物理消去証明ではない。
ローカル成果物は保存する。保存期限による自動削除は未実装。

Modalでは絶対的な請求上限を証明できないため、既定の `budget_guarantee=hard` は拒否する。
`reserved` は予約と寿命制御を明示的に選ぶ契約。実請求額が取れない場合、ゼロ扱いせず
予約額を保守的に計上し、`cost_basis=reserved_upper_bound` と表示する。
カタログの見積もりが実請求額を保証するわけではない。

制御の決定論とGPUモデル出力のビット単位再現性は別の契約。
検証成功は指定した入力と検査条件の範囲であり、任意の日本語記事の品質保証ではない。
今回の実装はModalのinfer/visionまで。学習・LoRA・MCP・追加providerは未実装。

設計・完成条件: [DETERMINISTIC_REBUILD.md](../docs/design/DETERMINISTIC_REBUILD.md)。
Modalの仕様: [Sandbox](https://modal.com/docs/sdk/py/latest/Sandbox)、
[料金](https://modal.com/pricing)、[timeout](https://modal.com/docs/guide/timeouts)。
