"""Machine-readable CLI over the same kernel and state as the HTTP service."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .api import create_app, wall_clock_ms
from .fixtures import FixtureProvider, fixture_bundle
from .kernel import Rejected, compile_plan
from .models import Bundle, Request, canonical
from .runtime import Runtime, public_job
from .store import Store


def load_json(path: Path, maximum=2*1024*1024) -> str:
    with path.open("rb") as handle:
        data = handle.read(maximum+1)
    if len(data) > maximum:
        raise Rejected("file_too_large")
    return data.decode()


def runtime_for(store, bundle):
    providers = {}
    if bundle.allow_fixture:
        providers["fixture"] = FixtureProvider(store.root / "fixture-remote")
    if any(o.execution.adapter == "modal" for o in bundle.snapshot.offers):
        from .modal_provider import ModalProvider
        providers["modal"] = ModalProvider()
    return Runtime(store, bundle, providers)


def demo() -> dict:
    now = wall_clock_ms()
    bundle = fixture_bundle(now)
    with tempfile.TemporaryDirectory(prefix="gpucall-demo-") as directory:
        store = Store(Path(directory), bundle.account_limit_microusd)
        runtime = runtime_for(store, bundle)
        request = Request(key="demo", profile="extract", instruction="決定論で実行するfixture",
                          deadline_ms=now+60000, budget_microusd=100)
        job = store.submit(request, bundle, now)
        for _ in range(10):
            runtime.tick(now)
        result = store.get(job["id"])
        return {"mode": "fixture_only", "real_gpu_calls": 0, "job": public_job(result),
                "result": json.loads(store.artifacts.get(result["output"])),
                "replay": store.replay(job["id"]), "account": store.account(), "audit": store.audit()}


def main():
    parser = argparse.ArgumentParser(prog="gpucall")
    parser.add_argument("--config", type=Path, default=Path(os.getenv("XDG_CONFIG_HOME", str(Path.home()/".config")))/"gpucall-next"/"config.json")
    parser.add_argument("--state", type=Path, default=Path(os.getenv("XDG_STATE_HOME", str(Path.home()/".local/state")))/"gpucall-next")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("fixture-config")
    commands.add_parser("validate-config")
    commands.add_parser("profiles")
    commands.add_parser("schema")
    builder = commands.add_parser("build-modal-image")
    builder.add_argument("--worker-dir", type=Path, required=True)
    builder.add_argument("--model", required=True)
    builder.add_argument("--revision", required=True)
    builder.add_argument("--app", default="gpucall-next")
    for name in ("submit", "estimate"):
        commands.add_parser(name).add_argument("request", type=Path)
    validation = commands.add_parser("validate-execution")
    validation.add_argument("request", type=Path)
    validation.add_argument("--execution", required=True)
    certification = commands.add_parser("certify")
    certification.add_argument("job")
    certification.add_argument("--valid-for-seconds", type=int, default=86400)
    for name in ("status", "result", "cancel", "replay"):
        commands.add_parser(name).add_argument("job")
    for name in ("tick", "jobs", "account", "audit"):
        commands.add_parser(name)
    commands.add_parser("import").add_argument("file", type=Path)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "::1"])
    serve.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.command == "demo":
            output = demo()
        elif args.command == "fixture-config":
            output = fixture_bundle(wall_clock_ms())
        elif args.command == "schema":
            output = Request.model_json_schema()
        elif args.command == "build-modal-image":
            from .image_builder import build_image
            output = build_image(args.worker_dir, args.model, args.revision, args.app)
        else:
            bundle = Bundle.model_validate_json(load_json(args.config))
            if args.command == "validate-config":
                output = {"valid": True, "profiles": len(bundle.profiles), "offers": len(bundle.snapshot.offers),
                          "fixture_enabled": bundle.allow_fixture}
            elif args.command == "profiles":
                output = {"profiles": [p.model_dump(mode="json") for p in bundle.profiles]}
            else:
                store = Store(args.state, bundle.account_limit_microusd)
                if args.command in {"submit", "estimate", "validate-execution"}:
                    request = Request.model_validate_json(load_json(args.request, 128*1024))
                    if args.command == "submit":
                        output = public_job(store.submit(request, bundle, wall_clock_ms()))
                    elif args.command == "validate-execution":
                        output = public_job(store.submit(request, bundle, wall_clock_ms(), validation_execution=args.execution))
                    else:
                        profile = next((p for p in bundle.profiles if p.id == request.profile), None)
                        if profile is None:
                            raise Rejected("unknown_profile")
                        output = compile_plan(request, profile, store.enrich(bundle).snapshot.model_copy(update={"at_ms": wall_clock_ms()}), allow_fixture=bundle.allow_fixture)
                elif args.command == "certify":
                    output = store.certify(args.job, args.valid_for_seconds*1000)
                elif args.command == "status":
                    output = public_job(store.get(args.job))
                elif args.command == "result":
                    job = store.get(args.job)
                    if job["output"] is None:
                        raise Rejected("result_not_available")
                    output = {"job": args.job, "text": store.artifacts.get(job["output"]).decode(), "status": job["status"]}
                elif args.command == "cancel":
                    output = public_job(store.cancel(args.job, wall_clock_ms()))
                elif args.command == "replay":
                    output = store.replay(args.job)
                elif args.command == "jobs":
                    output = [public_job(j) for j in store.jobs()]
                elif args.command == "account":
                    output = store.account()
                elif args.command == "audit":
                    output = store.audit()
                elif args.command == "import":
                    from .store import MAX_ARTIFACT_BYTES
                    with args.file.open("rb") as handle:
                        data = handle.read(MAX_ARTIFACT_BYTES+1)
                    output = {"sha256": store.artifacts.put(data), "size": len(data)}
                elif args.command == "tick":
                    output = {"changed": runtime_for(store, bundle).tick(wall_clock_ms())}
                elif args.command == "serve":
                    import uvicorn
                    token = os.getenv("GPUCALL_OWNER_TOKEN", "")
                    app = create_app(store, bundle, runtime_for(store, bundle), token=token, run_worker=True)
                    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
                    return
        print(canonical(output))
    except Rejected as exc:
        print(canonical({"error": {"code": exc.code, "details": exc.details}}))
        sys.exit(2)
    except Exception as exc:
        # Files and SDK exceptions may contain credentials or request contents.
        print(canonical({"error": {"code": type(exc).__name__}}))
        sys.exit(2)


if __name__ == "__main__":
    main()
