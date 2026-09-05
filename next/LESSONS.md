# 再構築中に確認した教訓

- 検証証跡は「有効期間内の成功を探す」実装にしない。最新の失敗が期限切れになっても、
  古い成功を復活させない。最新証跡を選んでから成否と有効期限を検査する。回帰テスト追加済み。
- Modal 1.5.5のSandboxFilesystemNotFoundErrorはFileNotFoundErrorの派生ではない。
  SDKソースで継承を確認し、SDK固有例外も明示して捕捉する。SDK形状のfixtureで検証する。
- Modal Image.uv_syncのvenvは `/.uv/.venv/`。想像したインストール先をworker起動に使わない。
- uvの親ディレクトリのPython選択がworkerへ伝播した。Python 3.12限定workerのlockは
  `uv lock --python 3.12` で対象を明示する。
- Modal App.lookupの戻り値は `.app_id` を持ち、`.object_id` は持たない。初回実接続で
  作成前にAttributeErrorになった。実SDKの属性検査とSandbox一覧0件で確認し、fixtureも修正。
# SDK resource names

Modal Sandbox names must be shorter than 64 characters. A full job hash plus an
attempt suffix exceeds this limit. Hash the provider name to 48 hex characters,
retain the complete attempt token in ownership tags, and test against the SDK's
actual validator before making a paid call.

# Installed CLI verification

`uv run` prepends the project virtualenv to PATH. A subprocess named `gpucall`
therefore tests the project executable even when its working directory is `/tmp`.
Use the absolute installed executable path when verifying the uv tool package.

# Vision dependency preflight

Transformers 4.57.6 AutoProcessor for Qwen2.5-VL initializes AutoVideoProcessor
even for still images, which requires torchvision. A public CPU-only processor
probe reproduced ImportError after the first GPU vision job failed. Pin
torchvision with torch in the worker lockfile and run preprocessing during image
build, before renting a GPU. Worker failures log only exception class and source
locations; messages and frame locals may contain private inputs.
