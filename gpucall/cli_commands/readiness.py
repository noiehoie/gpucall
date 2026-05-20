from __future__ import annotations

import argparse
from pathlib import Path

from gpucall.config import default_config_dir
from gpucall.readiness import build_readiness_report, dumps_readiness


def add_readiness_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("readiness")
    parser.add_argument("--config-dir", type=Path, default=default_config_dir())
    parser.add_argument("--source", default=None)
    parser.add_argument("--intent", default=None)
    parser.add_argument("--recipe", default=None)
    parser.add_argument("--validation-dir", type=Path, default=None)
    parser.add_argument("--live", action="store_true", help="refresh Provider Panopticon with live non-generation provider probes")
    return parser


def add_shipment_blockers_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("shipment-blockers")
    parser.add_argument("--config-dir", type=Path, default=default_config_dir())
    parser.add_argument("--intent", default=None)
    parser.add_argument("--recipe", default=None)
    parser.add_argument("--live", action="store_true", help="Include live provider checks")
    return parser


def run_readiness_command(args: argparse.Namespace) -> None:
    print(
        dumps_readiness(
            build_readiness_report(
                config_dir=args.config_dir,
                source=args.source,
                intent=args.intent,
                recipe=args.recipe,
                validation_dir=args.validation_dir,
                live=args.live,
            )
        ),
        end="",
    )


def run_shipment_blockers_command(args: argparse.Namespace) -> None:
    report = build_readiness_report(
        config_dir=args.config_dir,
        intent=args.intent,
        recipe=args.recipe,
        live=args.live,
    )
    print(f"{'RECIPE':<30} {'STATUS':<20} {'ACTION'}")
    print("-" * 80)
    for recipe in report.get("recipes", []):
        status = recipe.get("shipment_status", "unknown")
        name = recipe.get("recipe", "unknown")
        action = recipe.get("current_caller_action", "")
        print(f"{name:<30} {status:<20} {action}")
        if status != "shippable":
            for next_action in recipe.get("next_actions", []):
                print(f"  - {next_action}")
