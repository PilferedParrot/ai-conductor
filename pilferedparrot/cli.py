from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .budgets import collect_budgets
from .config import load_config
from .dispatch import dispatch
from .ledger import append_run
from .model import Conversation, PROVIDERS, ProviderBudget
from .qwen import ensure_qwen


def budget_text(budget: ProviderBudget) -> str:
    if not budget.available:
        return f"{budget.status_label} ({budget.note})" if budget.note else budget.status_label
    if budget.provider == "qwen":
        return budget.note or "local"
    windows = budget.windows or ((budget.window,) if budget.window else ())
    if windows:
        return "; ".join(
            f"{window.label or 'included usage'}: {window.remaining_percent:.0f}% left"
            for window in windows
        )
    return budget.note or "included usage unavailable"


def show_budgets(budgets: dict[str, ProviderBudget]) -> None:
    for name in PROVIDERS:
        print(f"{name}: {budget_text(budgets[name])}")


def run_prompt(
    prompt: str,
    provider: str,
    cwd: Path,
    conversation: Conversation,
    config: dict[str, Any],
) -> int:
    if provider == "qwen":
        ensure_qwen(config)
    print(f"→ {provider}")
    exit_code = dispatch(provider, prompt, cwd, conversation, config)
    append_run(
        config["ledger"], provider=provider, prompt=prompt, cwd=cwd,
        session_id=conversation.provider_session_id, budgets={}, exit_code=exit_code,
    )
    return exit_code


HELP = """Commands:
  /new             start a fresh conversation with the current provider
  /qwen             switch to a fresh local Qwen conversation
  /codex            switch to a fresh Codex conversation
  /budget           refresh Qwen status and Codex included usage
  /status           show the working directory and provider
  /help             show this help
  /quit             exit
"""


def repl(config: dict[str, Any], cwd: Path) -> int:
    provider = str(config["web"].get("default_provider", "codex"))
    if provider not in PROVIDERS:
        provider = "codex"
    conversation = Conversation(provider=provider)
    print(f"PilferedParrot {__version__} — direct local Qwen or OpenAI Codex sessions.")
    print("Type /help for commands. Ctrl-D exits.")
    while True:
        try:
            prompt = input(f"[{provider}] › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt.startswith("/"):
            command = prompt.partition(" ")[0]
            if command in ("/quit", "/exit"):
                return 0
            if command == "/new":
                conversation.reset(provider)
                print(f"new {provider} conversation")
            elif command in ("/qwen", "/codex"):
                provider = command[1:]
                conversation.reset(provider)
                print(f"new {provider} conversation")
            elif command == "/budget":
                show_budgets(collect_budgets(config))
            elif command == "/status":
                print(f"cwd: {cwd}\nprovider: {provider}")
            elif command == "/help":
                print(HELP)
            else:
                print(f"unknown command: {command}")
            continue
        try:
            run_prompt(prompt, provider, cwd, conversation, config)
        except Exception as exc:
            print(f"PilferedParrot error: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Direct interface for local Qwen and OpenAI Codex")
    parser.add_argument("--version", action="version", version=f"PilferedParrot {__version__}")
    parser.add_argument("--config", help="path to a JSON configuration file")
    parser.add_argument("--cwd", default=os.getcwd(), help="working directory for the agent")
    sub = parser.add_subparsers(dest="command")
    gui = sub.add_parser("gui", help="start the browser interface")
    gui.add_argument("--no-browser", action="store_true")
    gui.add_argument("--window-closed", metavar="URL", help=argparse.SUPPRESS)
    sub.add_parser("repl", help="start the terminal interface")
    sub.add_parser("budget", help="show local status and Codex included usage")
    run = sub.add_parser("run", help="send one prompt directly to a provider")
    run.add_argument("--provider", choices=PROVIDERS, default="codex")
    run.add_argument("prompt", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    window_url = getattr(args, "window_closed", None)
    if window_url is not None:
        from .web import _notify_window_closed
        return 0 if _notify_window_closed(window_url) else 1
    config = load_config(args.config)
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise SystemExit(f"not a directory: {cwd}")
    if args.command in (None, "gui"):
        from .web import serve
        no_browser = bool(getattr(args, "no_browser", False))
        return serve(config, cwd, open_browser=not no_browser)
    if args.command == "repl":
        return repl(config, cwd)
    if args.command == "budget":
        show_budgets(collect_budgets(config))
        return 0
    prompt = " ".join(args.prompt)
    return run_prompt(prompt, args.provider, cwd, Conversation(provider=args.provider), config)
