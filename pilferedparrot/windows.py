"""Windows entry point for both the portable executable and source launcher."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path


def state_directory() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "PilferedParrot"


def prepare_arguments(argv: list[str]) -> list[str]:
    """Keep configuration and projects outside the extracted application."""
    args = list(argv)
    if not any(value == "--config" or value.startswith("--config=") for value in args):
        state = state_directory()
        state.mkdir(parents=True, exist_ok=True)
        config_path = state / "config.json"
        try:
            with config_path.open("x", encoding="utf-8") as handle:
                json.dump({
                    "web": {"chat_store": str(state / "chats.json")},
                    "ledger": str(state / "runs.jsonl"),
                }, handle, indent=2)
                handle.write("\n")
        except FileExistsError:
            pass
        args = ["--config", str(config_path), *args]
    if not any(value == "--cwd" or value.startswith("--cwd=") for value in args):
        project = Path.home() / "PilferedParrot Projects"
        project.mkdir(parents=True, exist_ok=True)
        args = ["--cwd", str(project), *args]
    return args


def self_test() -> int:
    """Exercise the bundled server, assets and storage without provider access."""
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from . import __version__
    from .config import load_config
    from .ledger import append_run
    from .web import PilferedParrotApp, make_handler
    from .web_server import ASSET_NAMES, ASSET_ROOT

    with tempfile.TemporaryDirectory(prefix="pilferedparrot-self-test-") as directory:
        root = Path(directory)
        for name in ASSET_NAMES:
            if not (ASSET_ROOT / name).is_file():
                raise RuntimeError(f"The application bundle is missing {name}")
        config = load_config(root / "missing.json")
        config["web"].update({"chat_store": str(root / "chats.json"), "open_browser": False, "port": 0})
        config["ledger"] = str(root / "runs.jsonl")
        config["codex"].update({
            "config_path": str(root / "unused.toml"), "models_cache": str(root / "unused.json"),
        })
        app = PilferedParrotApp(config, root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(f"{origin}/api/status", timeout=5) as response:
                if json.load(response).get("service") != "pilferedparrot":
                    raise RuntimeError("Local HTTP server did not identify itself")
            with urlopen(origin, timeout=5) as response:
                if b"PilferedParrot" not in response.read():
                    raise RuntimeError("The interface did not load")
            rejected = Request(f"{origin}/api/chats", data=b"{}", headers={
                "Content-Type": "application/json", "Origin": origin,
            })
            try:
                with urlopen(rejected, timeout=5):
                    raise RuntimeError("Server accepted an unauthorized control request")
            except HTTPError as error:
                if error.code != 403:
                    raise
                error.close()
            chat = app.create_chat({"provider": "codex", "cwd": str(root)})
            if not chat.get("id") or not (root / "chats.json").is_file():
                raise RuntimeError("Session persistence failed")
            append_run(str(root / "runs.jsonl"), provider="codex", prompt="Self-test",
                       cwd=root, session_id=None, budgets={}, exit_code=0)
            if json.loads((root / "runs.jsonl").read_text(encoding="utf-8"))["exit_code"] != 0:
                raise RuntimeError("Run persistence failed")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            app.shutdown()
            server.server_close()
    print(json.dumps({"ok": True, "version": __version__, "checks": [
        "bundled-assets", "loopback-http", "control-authorization", "sessions", "run-ledger",
    ]}))
    return 0


def main(argv: list[str] | None = None) -> int:
    from .cli import main as cli_main

    args = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args == ["--self-test"]:
        return self_test()
    if any(arg in {"--help", "-h", "--version"} for arg in args):
        return cli_main(args)
    try:
        args = prepare_arguments(args)
        from .cli import build_parser
        parsed = build_parser().parse_args(args)
        if parsed.command in {None, "gui"} and not getattr(parsed, "no_browser", False):
            from .web_native import chromium_browser
            if chromium_browser() is None:
                raise RuntimeError("Install Google Chrome, Chromium, or Microsoft Edge to open the interface.")
        from .web_native import WindowsAppBrowser
        webbrowser.register("pilferedparrot-app", None, WindowsAppBrowser(), preferred=True)
        return cli_main(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"PilferedParrot could not start: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
