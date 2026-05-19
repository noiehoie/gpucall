from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from gpucall.config import default_config_dir
from gpucall.panopticon_service import (
    PANOPTICON_DEFAULT_HOST,
    PANOPTICON_DEFAULT_PORT,
    PANOPTICON_DEFAULT_REFRESH_INTERVAL_SECONDS,
    assert_safe_panopticon_host,
    create_panopticon_app,
    dumps_panopticon_report,
    refresh_panopticon,
    snapshot_panopticon,
)


def add_panopticon_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("panopticon")
    actions = parser.add_subparsers(dest="panopticon_action", required=True)

    snapshot = actions.add_parser("snapshot")
    snapshot.add_argument("--panopticon-path", type=Path, default=None)
    snapshot.add_argument("--output-json", type=Path, default=None)

    refresh = actions.add_parser("refresh")
    refresh.add_argument("--config-dir", type=Path, default=default_config_dir())
    refresh.add_argument("--panopticon-path", type=Path, default=None)
    refresh.add_argument("--tuple", dest="tuple_names", action="append", default=None)
    refresh.add_argument("--ttl-seconds", type=int, default=None)
    refresh.add_argument("--output-json", type=Path, default=None)

    serve = actions.add_parser("serve")
    serve.add_argument("--config-dir", type=Path, default=default_config_dir())
    serve.add_argument("--panopticon-path", type=Path, default=None)
    serve.add_argument("--host", default=PANOPTICON_DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=PANOPTICON_DEFAULT_PORT)
    serve.add_argument("--refresh-interval-seconds", type=int, default=PANOPTICON_DEFAULT_REFRESH_INTERVAL_SECONDS)
    serve.add_argument("--no-refresh-loop", action="store_true")
    return parser


def run_panopticon_command(args: argparse.Namespace) -> None:
    action = args.panopticon_action
    if action == "snapshot":
        _emit_report(snapshot_panopticon(panopticon_path=args.panopticon_path), args.output_json)
        return
    if action == "refresh":
        try:
            report = refresh_panopticon(
                config_dir=args.config_dir,
                panopticon_path=args.panopticon_path,
                tuple_names=args.tuple_names,
                ttl_seconds=args.ttl_seconds,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        _emit_report(report, args.output_json)
        return
    if action == "serve":
        try:
            assert_safe_panopticon_host(args.host)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        refresh_interval = None if args.no_refresh_loop else args.refresh_interval_seconds
        if refresh_interval is not None and refresh_interval < 1:
            raise SystemExit("provider panopticon refresh interval must be >= 1 second")
        app = create_panopticon_app(
            config_dir=args.config_dir,
            panopticon_path=args.panopticon_path,
            refresh_interval_seconds=refresh_interval,
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return
    raise SystemExit(f"unknown panopticon action: {action}")


def _emit_report(report: dict[str, object], output_json: Path | None) -> None:
    payload = dumps_panopticon_report(report)
    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(payload, encoding="utf-8")
    print(payload, end="")
