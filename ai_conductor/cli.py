from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .budgets import collect_budgets
from .config import load_config
from .dispatch import dispatch
from .ledger import append_run
from .model import Conversation, ProviderBudget
from .qwen import ensure_qwen
from .router import ask_qwen, enforce_constraints


def budget_text(budget: ProviderBudget) -> str:
    if not budget.available:
        return f"{budget.status_label} ({budget.note})" if budget.note else budget.status_label
    if budget.provider == "qwen":
        text = "local/no quota"
    elif budget.window:
        text = f"{budget.window.remaining_percent:.0f}% left"
    else:
        text = "budget unknown"
    if budget.note:
        text += f" ({budget.note})"
    return text


def show_budgets(budgets: dict[str, ProviderBudget]) -> None:
    print("  ".join(f"{name}: {budget_text(budgets[name])}" for name in ("qwen", "claude", "codex")))


def route_prompt(prompt: str, config: dict[str, Any], budgets: dict[str, ProviderBudget]):
    ensure_qwen(config)
    budgets["qwen"] = ProviderBudget("qwen", True, note="local; no subscription quota")
    decision = ask_qwen(prompt, budgets, config)
    provider, constraint_note = enforce_constraints(decision, budgets, config)
    suffix = f"; {constraint_note}" if constraint_note else ""
    print(f"→ {provider}: {decision.reason}{suffix}")
    return provider, decision


def run_prompt(
    prompt: str,
    forced: str | None,
    cwd: Path,
    conversation: Conversation,
    config: dict[str, Any],
) -> int:
    budgets = collect_budgets(config)
    if conversation.provider:
        provider = conversation.provider
        print(f"→ {provider} (pinned conversation)")
    elif forced:
        provider = forced
        if provider == "qwen":
            ensure_qwen(config)
        print(f"→ {provider} (manual override)")
    else:
        provider, _decision = route_prompt(prompt, config, budgets)
    exit_code = dispatch(provider, prompt, cwd, conversation, config)
    append_run(
        config["ledger"], provider=provider, prompt=prompt, cwd=cwd,
        session_id=conversation.provider_session_id, budgets=budgets, exit_code=exit_code,
    )
    return exit_code


HELP = """Commands:
  /new             end the current provider conversation and return to auto-routing
  /auto            same as /new
  /qwen|/claude|/codex  start a new conversation pinned to that provider
  /budget           refresh quota information
  /status           show the current working directory and provider pin
  /help             show this help
  /quit             exit
"""


def repl(config: dict[str, Any], cwd: Path) -> int:
    conversation = Conversation()
    forced: str | None = None
    print("AI Conductor 0.3 — Qwen routes and has local coding tools; follow-ups stay pinned.")
    print("Type /help for commands. Ctrl-D exits.")
    while True:
        label = conversation.provider or forced or "auto"
        try:
            prompt = input(f"[{label}] › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt.startswith("/"):
            command, _, argument = prompt.partition(" ")
            if command in ("/quit", "/exit"):
                return 0
            if command in ("/new", "/auto"):
                forced = None
                conversation.reset()
                print("new auto-routed task")
            elif command in ("/qwen", "/claude", "/codex"):
                forced = command[1:]
                conversation.reset()
                print(f"new task pinned to {forced}")
            elif command == "/budget":
                show_budgets(collect_budgets(config))
            elif command == "/status":
                print(f"cwd: {cwd}\nprovider: {conversation.provider or forced or 'auto'}")
            elif command == "/help":
                print(HELP)
            else:
                print(f"unknown command: {command}")
            continue
        try:
            run_prompt(prompt, forced, cwd, conversation, config)
        except Exception as exc:
            print(f"conductor error: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qwen-routed interface for Claude Code and Codex")
    parser.add_argument("--config", help="path to a JSON configuration file")
    parser.add_argument("--cwd", default=os.getcwd(), help="working directory for dispatched agents")
    sub = parser.add_subparsers(dest="command")
    gui = sub.add_parser("gui", help="start the Claude-style web interface")
    gui.add_argument("--no-browser", action="store_true")
    sub.add_parser("repl", help="start the terminal interface")
    sub.add_parser("budget", help="show current provider budgets")
    route = sub.add_parser("route", help="ask Qwen to route without dispatching")
    route.add_argument("prompt", nargs="+")
    run = sub.add_parser("run", help="route and dispatch one prompt")
    run.add_argument("--provider", choices=("auto", "qwen", "claude", "codex"), default="auto")
    run.add_argument("prompt", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    if args.command == "route":
        budgets = collect_budgets(config)
        provider, decision = route_prompt(prompt, config, budgets)
        print(json.dumps({"provider": provider, "decision": decision.__dict__}, default=list, indent=2))
        return 0
    forced = None if args.provider == "auto" else args.provider
    return run_prompt(prompt, forced, cwd, Conversation(), config)
