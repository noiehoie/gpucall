"""Build-time download of a pinned model revision; no execution-time network."""
import sys
import shutil
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

model, revision = sys.argv[1:]
info = HfApi().model_info(model, revision=revision, files_metadata=True)
size = sum(s.size or 0 for s in info.siblings)
if size > 32 * 1024**3:
    raise RuntimeError("model download exceeds 32 GiB build limit")
total, used, free = shutil.disk_usage("/")
if free - size < total // 20:
    raise RuntimeError("model download would exceed disk capacity limit")
snapshot_download(model, revision=revision, local_dir="/opt/gpucall-model",
                  ignore_patterns=["*.bin", "*.pt", "*.h5", "*.msgpack", "*.onnx"])
Path("/opt/gpucall-model/build-identity.json").write_text(
    __import__("json").dumps({"model": model, "revision": revision}))
