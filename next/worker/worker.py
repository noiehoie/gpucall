"""GPU workload only. No routing, price, retry, credential or provider decisions."""
from __future__ import annotations

import base64
import io
import json
import os
import time
from pathlib import Path

MAX_INPUT = 32 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024


def infer(payload):
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor, AutoTokenizer, Qwen2_5_VLForConditionalGeneration
    from PIL import Image

    torch.manual_seed(0)
    model_path = "/opt/gpucall-model"
    identity = json.loads(Path(model_path, "build-identity.json").read_text())
    if identity != {"model": payload.get("model"), "revision": payload.get("model_revision")}:
        raise ValueError("model identity mismatch")
    instruction = payload["instruction"]
    images = []
    for item in payload["inputs"]:
        data = base64.b64decode(item["data"], validate=True)
        if item["media_type"].startswith("image/"):
            image = Image.open(io.BytesIO(data))
            if image.width * image.height > 16_000_000:
                raise ValueError("image exceeds decoded pixel limit")
            images.append(image.convert("RGB"))
        else:
            instruction += "\n\n" + data.decode("utf-8")
    kwargs = {"local_files_only": True, "trust_remote_code": False, "torch_dtype": torch.float16}
    if payload["task"] == "vision":
        model = Qwen2_5_VLForConditionalGeneration.from_pretrained(model_path, **kwargs).to("cuda")
        processor = AutoProcessor.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
        content = [{"type": "image"} for _ in images] + [{"type": "text", "text": instruction}]
        text = processor.apply_chat_template([{"role": "user", "content": content}], tokenize=False, add_generation_prompt=True)
        encoded = processor(text=[text], images=images, return_tensors="pt").to("cuda")
        decoder = processor
    else:
        model = AutoModelForCausalLM.from_pretrained(model_path, **kwargs).to("cuda")
        decoder = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
        text = decoder.apply_chat_template([{"role": "user", "content": instruction}], tokenize=False, add_generation_prompt=True)
        encoded = decoder(text, return_tensors="pt").to("cuda")
    model.eval()
    maximum = min(int(payload["max_tokens"]), 32768)
    context = getattr(model.config, "max_position_embeddings", None)
    if context is not None and encoded.input_ids.shape[1] + maximum > context:
        raise ValueError("input exceeds model context")
    with torch.inference_mode():
        output = model.generate(**encoded, max_new_tokens=maximum, do_sample=False)
    generated = output[:, encoded.input_ids.shape[1]:]
    result = decoder.batch_decode(generated, skip_special_tokens=True)[0]
    if len(result.encode()) > min(int(payload["max_output_bytes"]), MAX_OUTPUT):
        raise ValueError("output exceeds byte limit")
    return result


def main():
    # Kept alive after completion so the owner can durably retrieve results before termination.
    # The provider-enforced Sandbox lifetime also bounds this wait if the gateway disappears.
    ready = Path("/tmp/gpucall-ready")
    while not ready.exists():
        time.sleep(0.1)
    try:
        path = Path("/tmp/gpucall-input.json")
        if path.stat().st_size > MAX_INPUT:
            raise ValueError("input too large")
        payload = json.loads(path.read_text())
        result = {"state": "succeeded", "output": infer(payload), "error": None, "actual_microusd": None}
    except Exception as exc:
        # No payload, image, prompt or model output in provider logs.
        # Exception messages and frame locals can contain input, so report only code locations.
        import sys
        import traceback
        frames = [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                  for f in traceback.extract_tb(exc.__traceback__)]
        print(json.dumps({"exception": type(exc).__name__, "frames": frames}), file=sys.stderr, flush=True)
        result = {"state": "failed", "error": "worker_failed", "actual_microusd": None}
    temporary = Path("/tmp/gpucall-result.tmp")
    with temporary.open("w") as handle:
        temporary.chmod(0o600)
        json.dump(result, handle, ensure_ascii=False, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace("/tmp/gpucall-result.json")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
