"""Bounded, public-input integration validation. Explicitly billable; no hidden retries."""
from __future__ import annotations

import argparse
import json
import os
import struct
import time
import zlib
from pathlib import Path

from gpucall.api import wall_clock_ms
from gpucall.cli import runtime_for
from gpucall.models import Bundle, Execution, Input, Offer, Profile, Request, Snapshot, canonical
from gpucall.runtime import public_job
from gpucall.store import Store


def blue_png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind+data))
    rows = (b"\x00" + b"\x00\x00\xff" * 256) * 256
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB",256,256,8,2,0,0,0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def run(args):
    now = wall_clock_ms()
    store = Store(args.state, 1_000_000)
    config = args.state / "config.json"
    previous = Bundle.model_validate_json(config.read_text()) if config.exists() else None
    profile = Profile(id="gpu-smoke-" + args.task, revision=1, task=args.task, max_tokens=32,
                      required_literals=("GPUCALL_OK" if args.task == "infer" else "BLUE",))
    execution = Execution(id="modal-" + args.task, adapter="modal", tasks=(args.task,), model=args.model,
                          model_revision=args.revision, image=args.image, gpu=args.gpu, region="us-east", trusted=True)
    offer = Offer(execution=execution, observed_ms=now, expires_ms=now+7200_000, ready=True,
                  rate_microusd_per_second=args.rate_microusd, expected_ms=30000,
                  maximum_ms=args.lifetime_seconds*1000, stop_grace_ms=10000,
                  price_source="https://modal.com/pricing", hard_bound=False)
    profiles = tuple(p for p in previous.profiles if p.id != profile.id) if previous else ()
    offers = tuple(o for o in previous.snapshot.offers if o.execution.id != execution.id) if previous else ()
    bundle = Bundle(profiles=(*profiles, profile), snapshot=Snapshot(at_ms=now, offers=(*offers, offer), evidence=()),
                    account_limit_microusd=1_000_000, max_running=1)
    encoded = canonical(bundle).encode()
    if config.exists():
        backup = config.with_name("config-"+str(now)+".json")
        with backup.open("xb") as handle:
            backup.chmod(0o600)
            handle.write(config.read_bytes())
    temporary = config.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        temporary.chmod(0o600)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(config)
    runtime = runtime_for(store, bundle)
    if args.task == "vision":
        image = blue_png()
        inputs = (Input(sha256=store.artifacts.put(image), size=len(image), media_type="image/png"),)
        instruction = "What is the dominant color in this image? Reply with exactly one word: RED, GREEN, or BLUE."
    else:
        inputs = ()
        instruction = "Reply with exactly GPUCALL_OK and nothing else."
    reports = []
    for phase in ("validation", "production"):
        start = wall_clock_ms()
        request = Request(key=f"{args.task}-{phase}-{now}", profile=profile.id, instruction=instruction, inputs=inputs,
                          deadline_ms=start+(args.lifetime_seconds+90)*1000, budget_microusd=250_000,
                          privacy="public", budget_guarantee="reserved")
        job = store.submit(request, bundle, start, validation_execution=execution.id if phase == "validation" else None)
        print(canonical({"phase": phase, "job": job["id"], "state": "submitted"}), flush=True)
        observed = None
        while wall_clock_ms() < request.deadline_ms + 60000:
            runtime.tick(wall_clock_ms())
            job = store.get(job["id"])
            state = (job["status"], job["error"])
            if state != observed:
                print(canonical({"phase": phase, "state": state}), flush=True)
                observed = state
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.5)
        report = {"phase": phase, "job": public_job(job), "replay": store.replay(job["id"]),
                  "elapsed_ms": wall_clock_ms()-start}
        if job["output"]:
            report["output"] = store.artifacts.get(job["output"]).decode()
        reports.append(report)
        report_path = args.state / f"{args.task}-{phase}-{now}-report.json"
        with report_path.open("w") as handle:
            report_path.chmod(0o600)
            handle.write(canonical(report))
        if phase == "validation" and job["status"] in {"succeeded", "failed", "cancelled"} and job["receipts"]:
            store.certify(job["id"], 86400_000)
        if job["status"] != "succeeded":
            raise RuntimeError("live validation failed; evidence preserved at " + str(report_path))
    print(canonical({"task": args.task, "passed": True, "reports": reports,
                     "account": store.account(), "audit": store.audit()}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--gpu", required=True)
    p.add_argument("--task", choices=["infer", "vision"], required=True)
    p.add_argument("--rate-microusd", type=int, required=True)
    p.add_argument("--lifetime-seconds", type=int, required=True)
    run(p.parse_args())
