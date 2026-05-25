from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from gpucall.config import default_config_dir, load_policy
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
from gpucall.panopticon_provisioning import (
    apply_provider_supply_provisioning_plan,
    build_provider_supply_provisioning_plan,
    dumps_provider_supply_provisioning_apply_result,
    dumps_provider_supply_provisioning_plan,
    load_provider_supply_provisioning_plan,
)
from gpucall.panopticon_remediation import (
    apply_remediation_plan,
    build_remediation_plan_from_path,
    dumps_apply_result,
    dumps_remediation_plan,
    load_remediation_plan,
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

    plan = actions.add_parser("plan")
    plan.add_argument("--config-dir", type=Path, default=default_config_dir())
    plan.add_argument("--panopticon-path", type=Path, default=None)
    plan.add_argument("--output-json", type=Path, default=None)

    apply = actions.add_parser("apply")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--yes", action="store_true", help="perform supported provider mutations from the explicit plan JSON")
    apply.add_argument("--output-json", type=Path, default=None)

    provision_plan = actions.add_parser("provision-plan")
    provision_plan.add_argument("--config-dir", type=Path, default=default_config_dir())
    provision_plan.add_argument("--tuple", dest="tuple_name", default=None)
    provision_plan.add_argument("--candidate", dest="candidate_name", default=None)
    provision_plan.add_argument("--review-json", type=Path, default=None)
    provision_plan.add_argument("--template-id", default=None)
    provision_plan.add_argument("--endpoint-name", default=None)
    provision_plan.add_argument("--template-name", default=None)
    provision_plan.add_argument("--gpu-type-id", dest="gpu_type_ids", action="append", default=None)
    provision_plan.add_argument("--workers-min", type=int, default=None)
    provision_plan.add_argument("--workers-max", type=int, default=None)
    provision_plan.add_argument("--network-volume-id", default=None)
    provision_plan.add_argument("--data-center-id", dest="data_center_ids", action="append", default=None)
    provision_plan.add_argument("--container-disk-gb", type=int, default=None)
    provision_plan.add_argument("--output-json", type=Path, default=None)

    provision_apply = actions.add_parser("provision-apply")
    provision_apply.add_argument("plan", type=Path)
    provision_apply.add_argument("--yes", action="store_true", help="perform supported provider supply mutations from the explicit plan JSON")
    provision_apply.add_argument("--output-json", type=Path, default=None)

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
    if action == "plan":
        try:
            policy = load_policy(args.config_dir)
            report = build_remediation_plan_from_path(
                policy=policy,
                panopticon_path=args.panopticon_path,
            )
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        _emit_text(dumps_remediation_plan(report), args.output_json)
        return
    if action == "apply":
        try:
            plan = load_remediation_plan(args.plan)
            report = apply_remediation_plan(plan, dry_run=not args.yes)
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        _emit_text(dumps_apply_result(report), args.output_json)
        return
    if action == "provision-plan":
        try:
            report = build_provider_supply_provisioning_plan(
                config_dir=args.config_dir,
                tuple_name=args.tuple_name,
                candidate_name=args.candidate_name,
                review_path=args.review_json,
                template_id=args.template_id,
                endpoint_name=args.endpoint_name,
                template_name=args.template_name,
                gpu_type_ids=args.gpu_type_ids,
                workers_min=args.workers_min,
                workers_max=args.workers_max,
                network_volume_id=args.network_volume_id,
                data_center_ids=args.data_center_ids,
                container_disk_gb=args.container_disk_gb,
            )
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        _emit_text(dumps_provider_supply_provisioning_plan(report), args.output_json)
        return
    if action == "provision-apply":
        try:
            plan = load_provider_supply_provisioning_plan(args.plan)
            report = apply_provider_supply_provisioning_plan(plan, dry_run=not args.yes)
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        _emit_text(dumps_provider_supply_provisioning_apply_result(report), args.output_json)
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
    _emit_text(payload, output_json)


def _emit_text(payload: str, output_json: Path | None) -> None:
    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(payload, encoding="utf-8")
    print(payload, end="")
