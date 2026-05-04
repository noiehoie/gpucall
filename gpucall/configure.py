from __future__ import annotations

import sys
import termios
import tty
from pathlib import Path

from gpucall.config import default_config_dir
from gpucall.credentials import configured_credentials
from gpucall.configure_registry import ConfigureTarget, configure_target, configure_targets
import gpucall.configure_targets  # noqa: F401 - registers configure targets


def configure_command(config_dir: Path | None = None) -> None:
    root = config_dir or default_config_dir()
    session_configured: list[str] = []
    print("Welcome to gpucall setup.")
    targets = configure_targets()
    while True:
        already = configured_credentials()
        try:
            selection = _select_provider(targets, already)
        except (EOFError, KeyboardInterrupt):
            break
        if selection in ("", "done", "q", "quit", "exit"):
            break
        target = configure_target(selection)
        if target is None:
            print(f"Unknown setup target: {selection}")
            continue
        ok = target.run(root)
        if ok:
            session_configured.append(selection)
            if target.success_message is not None:
                print(target.success_message or f"Saved {selection} credentials.")
        if not _prompt_yes_no("\nConfigure another item?", default_yes=False):
            break
    _print_next_steps(configured_credentials() or session_configured)


def _select_provider(targets: list[ConfigureTarget], already: list[str]) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return _select_provider_by_prompt(targets, already)
    return _select_provider_by_cursor(targets, already)


def _select_provider_by_prompt(targets: list[ConfigureTarget], already: list[str]) -> str:
    print("\nSelect item to configure:")
    for index, target in enumerate(targets, start=1):
        marker = "✓" if target.name in already else " "
        print(f"  {index}. {marker} {target.label}")
    print("  0. done  - finish setup")
    raw = input("> ").strip().lower()
    if raw.isdigit():
        index = int(raw)
        if index == 0:
            return "done"
        if 1 <= index <= len(targets):
            return targets[index - 1].name
    return raw


def _select_provider_by_cursor(targets: list[ConfigureTarget], already: list[str]) -> str:
    options: list[ConfigureTarget | str] = [*targets, "done"]
    selected = 0
    print("\nSelect item to configure:")
    _render_menu(options, already, selected)
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            key = sys.stdin.read(1)
            if key in ("\r", "\n"):
                print()
                return options[selected]
            if key in ("q", "Q", "\x03"):
                print()
                return "done"
            if key == "\x1b":
                key += sys.stdin.read(2)
            if key in ("\x1b[A", "k"):
                selected = (selected - 1) % len(options)
                _render_menu(options, already, selected, rewind=True)
            elif key in ("\x1b[B", "j"):
                selected = (selected + 1) % len(options)
                _render_menu(options, already, selected, rewind=True)
            elif key.isdigit():
                index = int(key)
                if index == 0:
                    print()
                    return "done"
                if 1 <= index <= len(targets):
                    print()
                    return targets[index - 1].name
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _render_menu(options: list[ConfigureTarget | str], already: list[str], selected: int, *, rewind: bool = False) -> None:
    if rewind:
        sys.stdout.write(f"\x1b[{len(options)}A")
    for index, option in enumerate(options):
        cursor = "›" if index == selected else " "
        if option == "done":
            label = "done  - finish setup"
            marker = " "
        else:
            label = option.label
            marker = "✓" if option.name in already else " "
        sys.stdout.write("\x1b[2K")
        sys.stdout.write(f"  {cursor} {index + 1 if option != 'done' else 0}. {marker} {label}\n")
    sys.stdout.flush()


def _prompt_yes_no(message: str, default_yes: bool = False) -> bool:
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    try:
        raw = input(message + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default_yes
    if not raw:
        return default_yes
    return raw in ("y", "yes")


def _print_next_steps(configured: list[str]) -> None:
    print()
    print("-" * 56)
    print("  Setup session finished.")
    if configured:
        print(f"  Configured: {', '.join(sorted(set(configured)))}")
    print("-" * 56)
    print()
    print("  Next steps:")
    print("    gpucall doctor")
    print("    gpucall explain-config text-infer-standard --mode async")
    print("    gpucall seed-liveness text-infer-standard --count 100")
    print()
    print("  Re-run setup any time with 'gpucall configure'.")
