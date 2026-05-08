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
            )
        ),
        end="",
    )
