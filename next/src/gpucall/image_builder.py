"""Explicit build operation; never imported by the decision kernel."""
from __future__ import annotations

import re
import shlex
from pathlib import Path


def build_image(worker_dir: Path, model: str, revision: str, app_name: str) -> dict:
    import modal
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("model must be a repository ID and revision must be a full commit hash")
    if not (worker_dir / "uv.lock").is_file():
        raise ValueError("worker lockfile is required")
    image = (modal.Image.debian_slim(python_version="3.12")
             .uv_sync(str(worker_dir), frozen=True, uv_version="0.11.6")
             .add_local_file(worker_dir / "download.py", "/opt/gpucall-worker/download.py", copy=True)
             .run_commands("/.uv/.venv/bin/python /opt/gpucall-worker/download.py " + shlex.quote(model) + " " + shlex.quote(revision))
             .add_local_file(worker_dir / "worker.py", "/opt/gpucall-worker/worker.py", copy=True)
             .add_local_file(worker_dir / "preflight.py", "/opt/gpucall-worker/preflight.py", copy=True)
             .run_commands("useradd --uid 10001 --create-home worker", "chmod -R a+rX /opt/gpucall-model /opt/gpucall-worker /.uv")
             .env({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"})
             .run_commands("/.uv/.venv/bin/python /opt/gpucall-worker/preflight.py")
             .dockerfile_commands("USER 10001:10001", "HEALTHCHECK --interval=30s --start-period=60s CMD test -f /tmp/gpucall-ready"))
    app = modal.App.lookup(app_name, create_if_missing=True)
    image.build(app)
    return {"app": app_name, "image": image.object_id, "model": model, "model_revision": revision}
