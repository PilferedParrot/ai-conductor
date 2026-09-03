import errno
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError

from pilferedparrot.budgets import (
    claude_budget_from_response, codex_budget_from_response, collect_budgets,
    _start_command_available, qwen_available, read_claude_budget, read_claude_status,
    read_codex_budget,
)
from pilferedparrot.config import (
    _SameOriginRedirectHandler, effective_model, load_config, model_catalog,
    open_compatible_url, provider_additional_dirs, resolve_command,
)
from pilferedparrot.dispatch import (
    RunCancelled, RunResult, _capture_process, _claude_command, _codex_command, _stream_process,
    capture_claude, capture_codex,
)
from pilferedparrot.model import (
    AUTH_LOCAL_NO_AUTH, AUTH_SIGNED_IN, AUTH_SIGNED_OUT, AUTH_UNKNOWN,
    PROVIDERS, REACHABLE, STATUS_CLI_MISSING, STATUS_SIGNED_OUT, UNREACHABLE,
    Conversation, ProviderBudget,
)
from pilferedparrot.qwen import _chat_completion, ensure_qwen, run_qwen_agent
from pilferedparrot.qwen_tools import QwenToolbox
from pilferedparrot.web import (
    API_GENERATION, ASSET_VERSION, CHROME_THEME_GALLERY_URL, RUNTIME_VERSION,
    ChatStore, PilferedParrotApp,
    _asset_fingerprint, _browser_url, _notify_window_closed,
    _pilferedparrot_dashboard_capability, _pilferedparrot_status,
    _runtime_fingerprint, _selected_chrome_theme,
    _terminate_stale_pilferedparrot, make_handler, serve,
)


def _web_config(directory):
    root = Path(directory)
    config = load_config(root / "missing-config.json")
    config["web"]["chat_store"] = str(root / "chats.json")
    config["ledger"] = str(root / "runs.jsonl")
    return config


class BudgetTests(unittest.TestCase):
    def test_qwen_start_command_status_uses_path_and_requires_executable_file(self):
        with patch("pilferedparrot.budgets.shutil.which", return_value="/usr/bin/qwen-start"):
            self.assertTrue(_start_command_available(["qwen-start", "--daemon"]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "start"
            path.write_text("#!/bin/sh\n")
            self.assertFalse(_start_command_available([str(path)]))
            path.chmod(0o700)
            self.assertTrue(_start_command_available([str(path)]))
            self.assertFalse(_start_command_available([directory]))

    def test_only_supported_providers_remain(self):
        self.assertEqual(PROVIDERS, ("qwen", "codex", "claude", "gemini"))
        self.assertIn("claude", load_config())
        self.assertIn("gemini", load_config())
        self.assertNotIn("routing", load_config())

    def test_codex_app_server_payload_has_truthful_window_labels(self):
        budget = codex_budget_from_response({
            "rateLimits": {
                "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1234},
                "secondary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 5678},
            }
        })
        self.assertEqual([window.label for window in budget.windows], [
            "5-hour included usage", "Weekly included usage",
        ])
        self.assertEqual([window.remaining_percent for window in budget.windows], [88, 98])
        self.assertNotIn("subscription", json.dumps(budget.as_dict()).lower())

    def test_percentage_strings_are_normalized_and_clamped(self):
        normalized = codex_budget_from_response({
            "rateLimits": {"primary": {
                "usedPercent": "21.5", "windowDurationMins": 300,
            }},
        })
        over = codex_budget_from_response({
            "rateLimits": {"primary": {"usedPercent": 140, "windowDurationMins": 300}},
        })
        under = codex_budget_from_response({
            "rateLimits": {"primary": {"usedPercent": -5, "windowDurationMins": 300}},
        })
        self.assertEqual(normalized.window.remaining_percent, 78.5)
        self.assertEqual(over.window.remaining_percent, 0)
        self.assertEqual(under.window.remaining_percent, 100)

    def test_codex_uses_more_constrained_window_as_summary(self):
        budget = codex_budget_from_response({
            "rateLimits": {
                "primary": {"usedPercent": 20, "windowDurationMins": 300},
                "secondary": {"usedPercent": 84, "windowDurationMins": 10080},
            }
        })
        self.assertEqual(budget.window.label, "Weekly included usage")
        self.assertEqual(budget.window.remaining_percent, 16)

    def test_codex_exposes_named_rate_limit_buckets(self):
        budget = codex_budget_from_response({
            "rateLimitsByLimitId": {
                "codex": {
                    "limitName": "Codex",
                    "primary": {"usedPercent": 25, "windowDurationMins": 300},
                },
                "codex_luna": {
                    "limitName": "Codex Luna",
                    "primary": {"usedPercent": 9, "windowDurationMins": 10080},
                },
            },
        })
        self.assertEqual([window.label for window in budget.windows], [
            "Codex · 5-hour included usage",
            "Codex Luna · Weekly included usage",
        ])

    def test_claude_usage_windows_are_normalized_as_percent_remaining(self):
        budget = claude_budget_from_response({
            "five_hour": {"utilization": 27.5, "resets_at": "2026-09-03T01:00:00Z"},
            "seven_day": {"utilization": 12, "resets_at": "2026-09-08T00:00:00Z"},
            "seven_day_opus": {"utilization": 140},
        }, observed_at=123)
        self.assertEqual([window.label for window in budget.windows], [
            "5-hour included usage", "Weekly included usage", "Weekly Opus usage",
        ])
        self.assertEqual(
            [window.remaining_percent for window in budget.windows], [72.5, 88, 0],
        )
        self.assertEqual(
            [window.resets_at for window in budget.windows[:2]],
            [1788397200, 1788825600],
        )
        self.assertEqual(budget.window.label, "Weekly Opus usage")
        self.assertEqual(budget.observed_at, 123)

    @patch("pilferedparrot.budgets.time.time", return_value=456)
    @patch("pilferedparrot.budgets.urllib.request.urlopen")
    @patch("pilferedparrot.budgets._claude_access_token", return_value="test-token")
    def test_claude_usage_probe_uses_oauth_endpoint_without_a_model_prompt(
        self, _token, open_url, _time,
    ):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "five_hour": {"utilization": 10, "resets_at": "2026-09-03T01:00:00Z"},
        }).encode()
        open_url.return_value.__enter__.return_value = response

        budget = read_claude_budget(load_config(Path("/definitely/missing/config.json")))

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(budget.window.remaining_percent, 90)
        self.assertEqual(budget.observed_at, 456)

    @patch("pilferedparrot.budgets.time.time", return_value=1_000)
    @patch("pilferedparrot.budgets.urllib.request.urlopen")
    def test_claude_usage_probe_refreshes_and_preserves_rotating_oauth_credentials(
        self, open_url, _time,
    ):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": directory}, clear=False,
        ):
            credentials = Path(directory) / ".credentials.json"
            credentials.write_text(json.dumps({
                "mcpOAuth": {"kept": True},
                "claudeAiOauth": {
                    "accessToken": "expired", "refreshToken": "refresh-old",
                    "expiresAt": 1, "scopes": ["user:profile"],
                },
            }))
            refreshed = MagicMock()
            refreshed.read.return_value = json.dumps({
                "access_token": "access-new", "refresh_token": "refresh-new",
                "expires_in": 3600, "refresh_token_expires_in": 7200,
                "scope": "user:profile user:sessions:claude_code",
            }).encode()
            usage = MagicMock()
            usage.read.return_value = json.dumps({
                "five_hour": {"utilization": 20},
            }).encode()
            open_url.side_effect = [
                MagicMock(__enter__=MagicMock(return_value=refreshed)),
                MagicMock(__enter__=MagicMock(return_value=usage)),
            ]

            budget = read_claude_budget(load_config(Path(directory) / "missing.json"))
            saved = json.loads(credentials.read_text())

        refresh_request = open_url.call_args_list[0].args[0]
        usage_request = open_url.call_args_list[1].args[0]
        self.assertEqual(refresh_request.full_url, "https://platform.claude.com/v1/oauth/token")
        self.assertEqual(usage_request.get_header("Authorization"), "Bearer access-new")
        self.assertEqual(saved["claudeAiOauth"]["refreshToken"], "refresh-new")
        self.assertEqual(saved["claudeAiOauth"]["expiresAt"], 4_600_000)
        self.assertEqual(saved["mcpOAuth"], {"kept": True})
        self.assertEqual(budget.window.remaining_percent, 80)


class CommandResolutionTests(unittest.TestCase):
    def test_committed_defaults_have_no_machine_specific_start_command(self):
        config = load_config(Path("/definitely/missing/config.json"))
        self.assertFalse(config["qwen"]["auto_start"])
        self.assertEqual(config["qwen"]["start_command"], [])
        self.assertFalse(config["qwen"]["allow_remote_egress"])
        self.assertEqual(config["web"]["default_provider"], "codex")

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_existing_config_is_repaired_to_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}")
            path.chmod(0o664)
            load_config(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @patch("pilferedparrot.qwen.urllib.request.urlopen")
    def test_qwen_remote_endpoints_are_rejected_before_network_requests(self, open_url):
        config = load_config(Path("/definitely/missing/config.json"))
        config["qwen"]["base_url"] = "https://example.com/v1"
        with self.assertRaisesRegex(ValueError, "allow_remote_egress"):
            _chat_completion([], config)
        open_url.assert_not_called()
        with self.assertRaisesRegex(ValueError, "allow_remote_egress"):
            ensure_qwen(config, notify=lambda _message: None)
        open_url.assert_not_called()
        config["qwen"]["base_url"] = "http://127.0.0.1:8080/v1"
        config["qwen"]["health_url"] = "https://example.com/health"
        with self.assertRaisesRegex(ValueError, "allow_remote_egress"):
            qwen_available(config)
        open_url.assert_not_called()

    @patch("pilferedparrot.config._compatible_opener")
    def test_qwen_remote_endpoints_require_explicit_opt_in(self, opener_factory):
        config = load_config(Path("/definitely/missing/config.json"))
        config["qwen"]["base_url"] = "https://example.com/v1"
        config["qwen"]["health_url"] = "https://example.com/health"
        config["qwen"]["allow_remote_egress"] = True
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
        opener_factory.return_value.open.return_value = response
        self.assertTrue(qwen_available(config))
        _chat_completion([], config)
        self.assertEqual(opener_factory.return_value.open.call_count, 2)

    def test_qwen_remote_egress_opt_in_must_be_json_true(self):
        config = load_config(Path("/definitely/missing/config.json"))
        config["qwen"]["base_url"] = "https://example.com/v1"
        config["qwen"]["allow_remote_egress"] = "false"
        with self.assertRaisesRegex(ValueError, "allow_remote_egress"):
            _chat_completion([], config)

    def test_compatible_redirect_cannot_carry_credentials_to_another_origin(self):
        request = urllib.request.Request(
            "https://models.example/v1/models", headers={"Authorization": "Bearer secret"},
        )
        handler = _SameOriginRedirectHandler()
        with self.assertRaisesRegex(HTTPError, "another origin") as raised:
            handler.redirect_request(
                request, io.BytesIO(), 302, "Found", {},
                "https://attacker.example/collect",
            )
        raised.exception.close()

    def test_api_key_rejects_plaintext_remote_provider_endpoint(self):
        config = {
            "remote": {
                "base_url": "http://models.example/v1", "api_key_env": "REMOTE_KEY",
            },
        }
        request = urllib.request.Request("http://models.example/v1/models")
        with patch.dict(os.environ, {"REMOTE_KEY": "secret"}), \
             patch("pilferedparrot.config._compatible_opener") as opener:
            with self.assertRaisesRegex(ValueError, "require HTTPS"):
                open_compatible_url(config, "remote", request, timeout=1)
        opener.assert_not_called()

    def test_custom_provider_ids_cannot_shadow_routes_or_builtins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for provider in ("codex", "has/slash", "Uppercase"):
                path.write_text(json.dumps({
                    "provider_definitions": {
                        provider: {"base_url": "https://example.com/v1"},
                    },
                }))
                with self.assertRaisesRegex(ValueError, "provider"):
                    load_config(path)

    @patch("pilferedparrot.config.urllib.request.build_opener")
    def test_local_qwen_ignores_proxy_environment_and_blocks_remote_redirects(self, build):
        config = load_config(Path("/definitely/missing/config.json"))
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"choices":[{"message":{"content":"ok"}}]}'
        build.return_value.open.return_value = response
        with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.example:8080", "NO_PROXY": ""}):
            _chat_completion([], config)
        handlers = build.call_args.args
        proxy = next(item for item in handlers if isinstance(item, urllib.request.ProxyHandler))
        redirect = next(
            item for item in handlers if isinstance(item, urllib.request.HTTPRedirectHandler)
        )
        self.assertEqual(proxy.proxies, {})
        request = urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions")
        with self.assertRaisesRegex(HTTPError, "remote endpoint was blocked") as raised:
            redirect.redirect_request(
                request, io.BytesIO(), 307, "redirect", {}, "https://example.com/collect",
            )
        raised.exception.close()

    def test_codex_model_catalog_uses_cli_default_and_visible_cached_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            cache_path = root / "models.json"
            config_path.write_text('model = "gpt-current"\n')
            cache_path.write_text(json.dumps({"models": [
                {"slug": "gpt-current", "display_name": "GPT Current", "visibility": "list"},
                {"slug": "gpt-other", "display_name": "GPT Other", "visibility": "list"},
                {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide"},
            ]}))
            config = load_config(root / "missing.json")
            config["codex"]["config_path"] = str(config_path)
            config["codex"]["models_cache"] = str(cache_path)
            self.assertEqual(effective_model(config, "codex"), "gpt-current")
            catalog = model_catalog(config)
        self.assertEqual(set(catalog), {"qwen", "codex", "claude", "gemini"})
        self.assertEqual(catalog["codex"]["options"], [
            {"value": "gpt-current", "label": "GPT Current"},
            {"value": "gpt-other", "label": "GPT Other"},
        ])

    def test_default_claude_catalog_has_only_numbered_models(self):
        catalog = model_catalog(load_config(Path("/definitely/missing/config.json")))
        self.assertEqual(catalog["claude"]["default"], None)
        self.assertEqual(catalog["claude"]["options"], [
            {"value": "claude-fable-5-1", "label": "Claude Fable 5.1",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-fable-5", "label": "Claude Fable 5",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-opus-5", "label": "Claude Opus 5",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-opus-4-8", "label": "Claude Opus 4.8",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-sonnet-5", "label": "Claude Sonnet 5",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-haiku-4-5", "label": "Claude Haiku 4.5",
             "context_window": 200_000, "max_context_window": 200_000},
        ])

    @patch("pilferedparrot.qwen.qwen_available", return_value=False)
    def test_qwen_auto_start_requires_explicit_command(self, _available):
        config = load_config()
        config["qwen"]["auto_start"] = True
        config["qwen"]["start_command"] = []
        with self.assertRaisesRegex(RuntimeError, "start_command is empty"):
            ensure_qwen(config, notify=lambda _message: None)

    @patch("pilferedparrot.qwen.qwen_available", return_value=False)
    def test_qwen_auto_start_command_obeys_startup_timeout(self, _available):
        config = load_config()
        config["qwen"].update({
            "auto_start": True,
            "start_command": ["/bin/sh", "-c", "sleep 30"],
            "start_timeout_seconds": 0.1,
        })
        with self.assertRaisesRegex(TimeoutError, "timed out"):
            ensure_qwen(config, notify=lambda _message: None)

    def test_found_in_extra_search_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o700)
            config = load_config()
            config["cli_search_paths"] = [directory]
            with patch("pilferedparrot.config.shutil.which", return_value=None):
                self.assertEqual(resolve_command(config, "codex"), str(executable))

    def test_explicit_path_never_falls_back_to_path(self):
        config = load_config()
        config["codex"]["command"] = "/missing/explicit/codex"
        with patch("pilferedparrot.config.shutil.which", return_value="/usr/bin/codex") as which:
            self.assertIsNone(resolve_command(config, "codex"))
        which.assert_not_called()

    @patch("pilferedparrot.budgets.resolve_command", return_value=None)
    def test_missing_codex_cli_is_not_called_offline(self, _resolve):
        budget = read_codex_budget(load_config())
        self.assertFalse(budget.available)
        self.assertEqual(budget.status, STATUS_CLI_MISSING)
        self.assertEqual(budget.auth_status, AUTH_UNKNOWN)
        self.assertEqual(budget.reachability, UNREACHABLE)
        self.assertNotIn("offline", budget.note.lower())

    @patch("pilferedparrot.budgets.resolve_command", return_value="/usr/bin/codex")
    @patch("pilferedparrot.budgets.subprocess.run")
    def test_signed_out_codex_is_distinct_from_missing(self, run, _resolve):
        run.return_value.returncode = 1
        run.return_value.stdout = "Not logged in"
        run.return_value.stderr = ""
        budget = read_codex_budget(load_config())
        self.assertFalse(budget.available)
        self.assertEqual(budget.status, STATUS_SIGNED_OUT)
        self.assertEqual(budget.auth_status, AUTH_SIGNED_OUT)
        self.assertEqual(budget.reachability, UNREACHABLE)

    @patch("pilferedparrot.budgets.resolve_command", return_value="/usr/bin/codex")
    @patch("pilferedparrot.budgets.subprocess.Popen", side_effect=OSError("down"))
    @patch("pilferedparrot.budgets.subprocess.run")
    def test_codex_endpoint_failure_preserves_verified_sign_in(
        self, run, _popen, _resolve,
    ):
        run.return_value.returncode = 0
        run.return_value.stdout = "Logged in"
        run.return_value.stderr = ""
        budget = read_codex_budget(load_config(Path("/definitely/missing/config.json")))
        self.assertEqual(budget.auth_status, AUTH_SIGNED_IN)
        self.assertEqual(budget.reachability, UNREACHABLE)

    def test_provider_status_separates_authentication_from_reachability(self):
        codex = codex_budget_from_response({"rateLimits": {}})
        self.assertEqual(codex.auth_status, AUTH_SIGNED_IN)
        self.assertEqual(codex.reachability, REACHABLE)
        with patch("pilferedparrot.budgets.qwen_available", return_value=True), \
             patch("pilferedparrot.budgets.read_codex_budget", return_value=codex), \
             patch("pilferedparrot.budgets.read_claude_status", return_value=ProviderBudget(
                 "claude", False, auth_status=AUTH_SIGNED_OUT,
             )):
            qwen = collect_budgets(load_config(Path("/definitely/missing/config.json")))["qwen"]
        self.assertEqual(qwen.auth_status, AUTH_LOCAL_NO_AUTH)
        self.assertEqual(qwen.reachability, REACHABLE)
        self.assertEqual(qwen.as_dict()["auth_status"], "local_no_auth")

    @patch("pilferedparrot.budgets.read_claude_budget")
    @patch("pilferedparrot.budgets.resolve_command", return_value="/usr/bin/claude")
    @patch("pilferedparrot.budgets.subprocess.run")
    def test_claude_auth_status_is_reported_without_exposing_account_details(
        self, run, _resolve, usage,
    ):
        run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({
                "loggedIn": True, "authMethod": "claude.ai", "email": "private@example.com",
            }), "",
        )
        usage.return_value = claude_budget_from_response({
            "five_hour": {"utilization": 25, "resets_at": "2026-09-03T01:00:00Z"},
        })
        status = read_claude_status(load_config(Path("/definitely/missing/config.json")))
        self.assertEqual(status.auth_status, AUTH_SIGNED_IN)
        self.assertEqual(status.reachability, REACHABLE)
        self.assertEqual(status.window.remaining_percent, 75)
        self.assertNotIn("private@example.com", status.note)

    @patch("pilferedparrot.budgets.read_claude_budget", side_effect=OSError("offline"))
    @patch("pilferedparrot.budgets.resolve_command", return_value="/usr/bin/claude")
    @patch("pilferedparrot.budgets.subprocess.run")
    def test_claude_usage_failure_preserves_verified_sign_in(
        self, run, _resolve, _usage,
    ):
        run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), "",
        )
        status = read_claude_status(load_config(Path("/definitely/missing/config.json")))
        self.assertTrue(status.available)
        self.assertEqual(status.auth_status, AUTH_SIGNED_IN)
        self.assertEqual(status.reachability, REACHABLE)
        self.assertIn("usage unavailable", status.note)


class QwenToolTests(unittest.TestCase):
    def _config(self):
        config = load_config()["qwen"]
        config["shell_network"] = False
        return config

    def _require_working_bwrap(self):
        """Skip integration checks when this outer sandbox blocks namespaces."""
        probe = subprocess.run(
            [
                "/usr/bin/bwrap", "--die-with-parent", "--new-session",
                "--unshare-pid", "--unshare-net", "--ro-bind", "/", "/",
                "--dev", "/dev", "--proc", "/proc", "/bin/true",
            ],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        if probe.returncode:
            self.skipTest(f"bubblewrap cannot create its sandbox here: {probe.stdout.strip()}")

    def test_file_edit_returns_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            path.write_text("before\n")
            toolbox = QwenToolbox(Path(directory), self._config())
            result = toolbox.execute("edit_file", {
                "path": "sample.txt", "old_text": "before", "new_text": "after",
            })
            self.assertIn("-before", result)
            self.assertIn("+after", result)

    def test_read_only_chat_toolbox_rejects_mutating_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config()
            config["read_only"] = True
            toolbox = QwenToolbox(Path(directory), config)
            with self.assertRaisesRegex(PermissionError, "read-only Chat"):
                toolbox.execute("write_file", {"path": "blocked.txt", "content": "no"})
            self.assertFalse((Path(directory) / "blocked.txt").exists())

    def test_file_tool_rejects_workspace_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            toolbox = QwenToolbox(Path(directory), self._config())
            with self.assertRaises(PermissionError):
                toolbox.execute("read_file", {"path": "../secret"})

    def test_entire_home_workspace_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, "allow_home_workspace"):
            QwenToolbox(Path.home(), self._config())
        config = self._config()
        config["allow_home_workspace"] = True
        self.assertEqual(QwenToolbox(Path.home(), config).cwd, Path.home().resolve())

    def test_home_and_network_security_opt_ins_require_json_true(self):
        for value in ("false", 1, None):
            config = self._config()
            config["allow_home_workspace"] = value
            with self.assertRaisesRegex(ValueError, "allow_home_workspace"):
                QwenToolbox(Path.home(), config)
        with tempfile.TemporaryDirectory() as directory:
            config = self._config()
            config["shell_network"] = "false"
            toolbox = QwenToolbox(Path(directory), config)
            completed = subprocess.CompletedProcess([], 0, "", "")
            with patch("pilferedparrot.qwen_tools.shutil.which", return_value="/usr/bin/bwrap"), \
                 patch("pilferedparrot.qwen_tools.subprocess.run", return_value=completed) as run:
                toolbox.execute("shell", {"command": "true"})
            self.assertIn("--unshare-net", run.call_args.args[0])

    def test_workspace_cannot_be_a_parent_of_home(self):
        config = self._config()
        config["allow_home_workspace"] = True
        with self.assertRaisesRegex(ValueError, "parent of the home directory"):
            QwenToolbox(Path.home().parent, config)

    def test_additional_root_is_reachable_by_absolute_path(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as extra:
            shared = Path(extra) / "notes.txt"
            shared.write_text("shared-material\n")
            toolbox = QwenToolbox(Path(workspace), self._config(), [Path(extra)])
            self.assertIn("shared-material", toolbox.execute("read_file", {"path": str(shared)}))

    def test_additional_root_does_not_widen_to_its_parent(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as outside:
            extra = Path(outside) / "project"
            extra.mkdir()
            secret = Path(outside) / "secret.txt"
            secret.write_text("do-not-leak")
            toolbox = QwenToolbox(Path(workspace), self._config(), [extra])
            with self.assertRaises(PermissionError):
                toolbox.execute("read_file", {"path": str(secret)})

    def test_diff_accepts_a_path_in_an_additional_root(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as extra:
            shared = Path(extra) / "notes.txt"
            toolbox = QwenToolbox(Path(workspace), self._config(), [Path(extra)])
            toolbox.execute("write_file", {"path": str(shared), "content": "shared-material\n"})
            result = toolbox.execute("diff", {"path": str(shared)})
            self.assertIn("shared-material", result)

    def test_toolbox_rejects_an_additional_root_containing_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            simulated_home = root / "home"
            simulated_home.mkdir()
            workspace = simulated_home / "workspace"
            workspace.mkdir()
            with patch("pilferedparrot.qwen_tools.Path.home", return_value=simulated_home):
                with self.assertRaisesRegex(ValueError, "parents"):
                    QwenToolbox(workspace, self._config(), [root])

    def test_additional_dirs_cannot_name_the_home_directory(self):
        """An extra root must not become an indirect home mask bypass."""
        config = load_config()
        config["qwen"]["additional_dirs"] = [str(Path.home())]
        with self.assertRaisesRegex(ValueError, "home directory"):
            provider_additional_dirs(config, "qwen")
        # Still refused when the workspace opt-in is on: that setting is a
        # decision about the workspace, not a blanket grant.
        config["qwen"]["allow_home_workspace"] = True
        with self.assertRaisesRegex(ValueError, "home directory"):
            provider_additional_dirs(config, "qwen")

    def test_additional_dirs_cannot_name_an_ancestor_of_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            simulated_home = root / "home"
            simulated_home.mkdir()
            config = load_config()
            config["qwen"]["additional_dirs"] = [str(root)]
            with patch("pilferedparrot.config.Path.home", return_value=simulated_home):
                with self.assertRaisesRegex(ValueError, "parent of the home directory"):
                    provider_additional_dirs(config, "qwen")

    @unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "bubblewrap is not installed")
    def test_shell_reaches_additional_root_but_not_other_home_data(self):
        self._require_working_bwrap()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            extra = root / "library"
            extra.mkdir()
            (extra / "ok.txt").write_text("reachable-root")
            secret = root / "secret.txt"
            secret.write_text("do-not-leak")
            with patch("pilferedparrot.qwen_tools.Path.home", return_value=root):
                toolbox = QwenToolbox(workspace, self._config(), [extra])
                result = toolbox.execute("shell", {
                    "command": f"cat {extra}/ok.txt; head -c 20 {secret}",
                })
            self.assertIn("reachable-root", result)
            self.assertNotIn("do-not-leak", result)

    def test_file_tool_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory, \
             tempfile.TemporaryDirectory() as outside:
            Path(directory, "escape").symlink_to(outside, target_is_directory=True)
            toolbox = QwenToolbox(Path(directory), self._config())
            with self.assertRaises(PermissionError):
                toolbox.execute("write_file", {"path": "escape/payload", "content": "blocked"})
            self.assertFalse(Path(outside, "payload").exists())

    @unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "bubblewrap is not installed")
    def test_shell_cannot_read_sibling_home_data(self):
        self._require_working_bwrap()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            secret = root / "secret.txt"
            secret.write_text("do-not-leak")
            with patch("pilferedparrot.qwen_tools.Path.home", return_value=root):
                toolbox = QwenToolbox(workspace, self._config())
                result = toolbox.execute("shell", {"command": f"head -c 20 {secret}"})
            self.assertNotIn("do-not-leak", result)

    @patch("pilferedparrot.qwen._chat_completion")
    def test_agent_executes_tool_call_and_keeps_protocol_messages(self, complete):
        complete.side_effect = [
            {"content": "Checking.", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"sample.txt"}'},
            }]},
            {"content": "Done."},
        ]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "sample.txt").write_text("hello\n")
            messages = []
            result = run_qwen_agent("read it", messages, load_config(), Path(directory))
        self.assertEqual(result, "Done.")
        self.assertEqual([message["role"] for message in messages], [
            "user", "assistant", "tool", "assistant",
        ])


class CodexDispatchTests(unittest.TestCase):
    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_fresh_command_sets_primary_and_additional_write_roots(self, _command):
        with tempfile.TemporaryDirectory() as workspace, \
             tempfile.TemporaryDirectory() as additional:
            config = load_config(Path(workspace) / "missing.json")
            config["codex"]["additional_write_dirs"] = [additional]
            command = _codex_command(Conversation(), config, Path(workspace))
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertEqual(command[command.index("--cd") + 1], workspace)
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(command[command.index("--add-dir") + 1], additional)
        self.assertNotIn("resume", command)

    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_resumed_command_reasserts_workspace_policy_before_resume(self, _command):
        with tempfile.TemporaryDirectory() as workspace, \
             tempfile.TemporaryDirectory() as additional:
            config = load_config(Path(workspace) / "missing.json")
            config["codex"]["additional_write_dirs"] = [additional]
            command = _codex_command(
                Conversation(provider_session_id="thread-1"), config, Path(workspace),
            )
        resume = command.index("resume")
        self.assertLess(command.index("--cd"), resume)
        self.assertLess(command.index("--sandbox"), resume)
        self.assertLess(command.index("--add-dir"), resume)
        self.assertEqual(command[resume:], ["resume", "thread-1", "-"])

    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_missing_additional_write_root_fails_before_codex_starts(self, _command):
        config = load_config(Path("/definitely/missing/config.json"))
        config["codex"]["additional_write_dirs"] = ["/definitely/missing/project"]
        with self.assertRaisesRegex(ValueError, "does not exist"):
            _codex_command(Conversation(), config, Path.cwd())

    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_invocation_can_override_codex_reasoning_effort(self, _command):
        config = load_config(Path("/definitely/missing/config.json"))
        config["codex"]["reasoning_effort"] = "low"
        command = _codex_command(Conversation(), config, Path.cwd())
        override = command.index("--config")
        self.assertEqual(command[override + 1], 'model_reasoning_effort="low"')

    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_invocation_applies_selected_context_window_share(self, _command):
        config = load_config(Path("/definitely/missing/config.json"))
        config["codex"]["context_window_tokens"] = 872_000
        config["codex"]["context_window_percent"] = 50
        command = _codex_command(Conversation(), config, Path.cwd())
        self.assertIn("model_context_window=436000", command)

    @patch("pilferedparrot.dispatch._stream_process")
    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_codex_receives_raw_user_prompt_without_workspace_wrapper(self, _command, stream):
        def emit(_argv, prompt, _cwd, **kwargs):
            self.assertEqual(prompt, "do the work")
            kwargs["stdout_line"](json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n")
            kwargs["stdout_line"](json.dumps({
                "type": "item.completed", "item": {"type": "agent_message", "text": "Finished."},
            }) + "\n")
            return subprocess.CompletedProcess([], 0, "", "")

        stream.side_effect = emit
        result = capture_codex("do the work", Path.cwd(), __import__(
            "pilferedparrot.model", fromlist=["Conversation"]
        ).Conversation(), load_config(Path("/definitely/missing/config.json")))
        self.assertEqual(result.text, "Finished.")
        self.assertEqual(result.session_id, "t-1")

    @patch("pilferedparrot.dispatch._stream_process")
    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_codex_keeps_only_final_agent_message(self, _command, stream):
        def emit(_argv, _prompt, _cwd, **kwargs):
            for text in ("intermediate", "final"):
                kwargs["stdout_line"](json.dumps({
                    "type": "item.completed", "item": {"type": "agent_message", "text": text},
                }) + "\n")
            return subprocess.CompletedProcess([], 0, "", "")

        stream.side_effect = emit
        from pilferedparrot.model import Conversation
        result = capture_codex(
            "work", Path.cwd(), Conversation(),
            load_config(Path("/definitely/missing/config.json")),
        )
        self.assertEqual(result.text, "final")


class WebStoreTests(unittest.TestCase):
    def _wait_for_chat(self, app, chat_id, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with app.store.lock:
                chat = app.store.public(app.store.get(chat_id))
            if not any(message.get("pending") for message in chat["messages"]):
                return chat
            time.sleep(0.01)
        self.fail("chat did not finish")

    def _wait_for_chat_reply(self, app, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chat_thread = app.store.chat_public()
            if not chat_thread["pending"]:
                return chat_thread
            time.sleep(0.01)
        self.fail("Chat did not finish")

    def test_chat_store_persists_without_internal_provider_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            store = ChatStore(path)
            public = store.create(Path(directory), "qwen")
            with store.lock:
                stored = store.get(public["id"])
                stored["provider_messages"] = [{"role": "user", "content": "secret internal"}]
                store.save()
            self.assertNotIn("provider_messages", store.list_public()[0])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_chat_store_is_never_replaced_with_empty_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            original = b'{"chats": [truncated'
            path.write_bytes(original)
            with self.assertRaisesRegex(RuntimeError, "safely load chat history"):
                ChatStore(path)
            self.assertEqual(path.read_bytes(), original)

            path.write_text('{"chats": [null]}')
            with self.assertRaisesRegex(RuntimeError, "invalid work session"):
                ChatStore(path)
            self.assertEqual(path.read_text(), '{"chats": [null]}')

            future = '{"version": 999, "chats": []}'
            path.write_text(future)
            with self.assertRaisesRegex(RuntimeError, "unsupported format"):
                ChatStore(path)
            self.assertEqual(path.read_text(), future)

    def test_work_and_chat_messages_preserve_client_request_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            work = app.create_chat({"provider": "codex", "cwd": directory})
            with patch("pilferedparrot.web.threading.Thread.start"):
                updated = app.send_message(work["id"], {
                    "content": "hello", "request_id": "request-work-1234",
                })
                chat = app.send_chat_message({
                    "content": "hello", "request_id": "request-chat-1234",
                })
            self.assertEqual(updated["messages"][-2]["id"], "request-work-1234")
            self.assertEqual(chat["messages"][-2]["id"], "request-chat-1234")

    def test_legacy_qwen_history_and_ephemeral_claude_window_are_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            path.write_text(json.dumps({"chats": [{
                "id": "old", "window_id": "random-window-id",
                "requested_provider": "claude", "provider": "claude",
                "qwen_messages": [{"role": "user", "content": "legacy"}],
                "messages": [], "updated_at": 1,
            }]}))
            store = ChatStore(path)
            private = store.get("old")
        self.assertEqual(private["window_id"], "provider-claude")
        self.assertEqual(private["provider_messages"][0]["content"], "legacy")
        self.assertNotIn("qwen_messages", private)

    def test_legacy_routed_chat_becomes_direct_codex_without_erasing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            path.write_text(json.dumps({"chats": [{
                "id": "old", "requested_provider": "auto", "requested_model": "opus",
                "provider": "claude", "messages": [{
                    "role": "assistant", "provider": "claude", "content": "historical",
                }], "updated_at": 1,
            }]}))
            store = ChatStore(path)
            chat = store.list_public()[0]
        self.assertEqual(chat["requested_provider"], "codex")
        self.assertIsNone(chat["requested_model"])
        self.assertEqual(chat["messages"][0]["content"], "historical")

    def test_state_contains_only_direct_provider_catalogs(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            state = app.state("dashboard")
            chat_state = app.state("chat")
        self.assertEqual(set(state["models"]), {"qwen", "codex", "claude", "gemini"})
        self.assertEqual(set(state["model_catalog"]), {"qwen", "codex", "claude", "gemini"})
        self.assertEqual(
            {provider["id"] for provider in state["providers"]},
            {"qwen", "codex", "claude", "gemini"},
        )
        claude = next(provider for provider in state["providers"] if provider["id"] == "claude")
        self.assertEqual(claude["auth_mode"], "cli")
        self.assertIn("claude.ai", claude["login_help"])
        gemini = next(provider for provider in state["providers"] if provider["id"] == "gemini")
        self.assertTrue(gemini["capabilities"]["resume"])
        self.assertTrue(gemini["capabilities"]["models"])
        self.assertEqual(state["default_provider"], "codex")
        self.assertEqual(state["runtime_version"], RUNTIME_VERSION)
        self.assertEqual(chat_state["chat_model"], "gpt-5.6-terra")
        self.assertEqual(chat_state["chat"]["messages"], [])
        self.assertFalse(chat_state["chat"]["pending"])
        self.assertEqual(chat_state["chat_history"], [])

    def test_dashboard_state_and_actions_are_isolated_by_window_and_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            main = app.create_chat(
                {"provider": "codex", "cwd": directory},
                window_id="main", window_provider="codex",
            )
            claude_a = app.create_chat(
                {"provider": "qwen", "cwd": directory},
                window_id="claude-a", window_provider="claude",
            )
            claude_b = app.create_chat(
                {"provider": "claude", "cwd": directory},
                window_id="claude-b", window_provider="claude",
            )

            main_state = app.state(
                "dashboard", window_id="main", window_provider="codex",
            )
            first_state = app.state(
                "dashboard", window_id="claude-a", window_provider="claude",
            )
            second_state = app.state(
                "dashboard", window_id="claude-b", window_provider="claude",
            )

            self.assertEqual([item["id"] for item in main_state["chats"]], [main["id"]])
            self.assertEqual([item["id"] for item in first_state["chats"]], [claude_a["id"]])
            self.assertEqual([item["id"] for item in second_state["chats"]], [claude_b["id"]])
            self.assertEqual(claude_a["requested_provider"], "claude")
            with self.assertRaises(KeyError):
                app.set_context_window(
                    claude_b["id"], {"percent": 50}, window_id="claude-a",
                )

    def test_new_work_session_uses_session_title(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
        self.assertEqual(chat["title"], "New work session")

    def test_persisted_legacy_repository_path_is_migrated_without_history_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pilfered Parrot"
            (root / "pilferedparrot").mkdir(parents=True)
            path = Path(directory) / "chats.json"
            messages = [{"role": "user", "content": "preserve this"}]
            path.write_text(json.dumps({"chats": [{
                "id": "legacy", "cwd": str(root.parent / "ai-conductor"),
                "requested_provider": "codex", "messages": messages,
                "updated_at": 1,
            }]}))
            with patch("pilferedparrot.web.RUNTIME_ROOT", root / "pilferedparrot"):
                app = PilferedParrotApp(_web_config(directory), root)
                migrated = app.store.get("legacy")
                created = app.create_chat({
                    "provider": "codex", "cwd": str(root.parent / "ai-conductor"),
                })
                persisted = json.loads(path.read_text())["chats"][0]

            self.assertEqual(migrated["cwd"], str(root.resolve()))
            self.assertEqual(migrated["messages"], messages)
            self.assertEqual(persisted["cwd"], str(root.resolve()))
            self.assertEqual(persisted["messages"], messages)
            self.assertEqual(created["cwd"], str(root.resolve()))

    def test_new_work_session_preserves_valid_current_project_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pilfered Parrot"
            (root / "pilferedparrot").mkdir(parents=True)
            project = root / "selected-project"
            project.mkdir()
            app = PilferedParrotApp(_web_config(directory), root)
            created = app.create_chat({"provider": "codex", "cwd": str(project)})
        self.assertEqual(created["cwd"], str(project.resolve()))

    def test_new_work_session_rejects_unrelated_nonexistent_project_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pilfered Parrot"
            (root / "pilferedparrot").mkdir(parents=True)
            app = PilferedParrotApp(_web_config(directory), root)
            missing = Path(directory) / "unrelated-missing-project"
            with self.assertRaisesRegex(ValueError, "project folder does not exist"):
                app.create_chat({"provider": "codex", "cwd": str(missing)})

    def test_new_technical_work_session_preserves_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            with app.store.lock:
                chat_thread = app.store.data["chat"]
                chat_thread["messages"] = [{"role": "user", "content": "keep this chat"}]
                app.store.save()
                chat_id = chat_thread["id"]

            work = app.create_chat({"provider": "codex", "cwd": directory})

            self.assertEqual(app.store.data["chat"]["id"], chat_id)
            self.assertEqual(
                app.store.chat_public()["messages"][0]["content"], "keep this chat",
            )
            self.assertEqual(app.store.get(work["id"])["messages"], [])

    def test_chat_relays_raw_content_to_read_only_terra_and_persists_separate_session(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            seen = []

            def dispatch(provider, prompt, cwd, conversation, config, _cancel):
                seen.append((provider, prompt, cwd, conversation.provider_session_id, config))
                return RunResult("I can keep an eye on that.", 0, "terra-thread")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch):
                pending = app.send_chat_message({"content": "What is technical doing?"})
                self.assertTrue(pending["pending"])
                chat_thread = self._wait_for_chat_reply(app)

            self.assertEqual(chat_thread["messages"][-1]["content"], "I can keep an eye on that.")
            self.assertEqual(seen[0][0], "codex")
            self.assertEqual(seen[0][1], "What is technical doing?")
            self.assertNotIn("TECHNICAL_STATE", seen[0][1])
            self.assertEqual(seen[0][2], Path(directory))
            self.assertIsNone(seen[0][3])
            self.assertEqual(seen[0][4]["codex"]["model"], "gpt-5.6-terra")
            self.assertEqual(seen[0][4]["codex"]["reasoning_effort"], "low")
            self.assertEqual(seen[0][4]["codex"]["sandbox"], "read-only")
            self.assertEqual(seen[0][4]["codex"]["additional_write_dirs"], [])

            reloaded = ChatStore(Path(directory) / "chats.json")
            self.assertNotIn("provider_session_id", reloaded.chat_public())
            self.assertEqual(reloaded.chat_public()["messages"][-1]["model"], "gpt-5.6-terra")
            self.assertEqual(reloaded.data["chat"]["provider_session_id"], "terra-thread")
            self.assertEqual(reloaded.get(chat["id"])["messages"], [])

    def test_technical_and_chat_histories_remain_separate_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _web_config(directory)
            app = PilferedParrotApp(config, Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            with app.store.lock:
                app.store.get(chat["id"])["messages"] = [{
                    "id": "technical", "role": "user", "content": "Fix the build",
                }]
                app.store.data["chat"].update({
                    "provider_session_id": "parrot-session",
                    "messages": [{"id": "chat", "role": "user", "content": "Keep me posted"}],
                })
                app.store.save()

            restarted = PilferedParrotApp(config, Path(directory))
            self.assertEqual(
                restarted.store.get(chat["id"])["messages"][0]["content"], "Fix the build",
            )
            self.assertEqual(
                restarted.store.chat_public()["messages"][0]["content"], "Keep me posted",
            )
            self.assertEqual(
                restarted.store.data["chat"]["provider_session_id"], "parrot-session",
            )

    def test_chat_does_not_interpret_control_protocol_or_affect_technical_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            answer = (
                "Stopping it now.\nPARROT_RELAY_CONTROL: "
                f'{{"action":"interrupt","chat_id":"{chat["id"]}"}}'
            )
            with patch("pilferedparrot.web.capture_dispatch", return_value=RunResult(
                answer, 0, "terra-thread",
            )), patch.object(app, "cancel_message", return_value=chat) as cancel:
                app.send_chat_message({"content": "Stop technical now"})
                chat_thread = self._wait_for_chat_reply(app)
            cancel.assert_not_called()
            self.assertEqual(chat_thread["messages"][-1]["content"], answer)
            self.assertNotIn("control_action", chat_thread["messages"][-1])

    def test_chat_can_cancel_its_own_response(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))

            def wait_for_cancel(_provider, _prompt, _cwd, _conversation, _config, cancel):
                while not cancel.is_set():
                    time.sleep(0.005)
                raise RunCancelled("cancelled")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=wait_for_cancel):
                app.send_chat_message({"content": "Never mind"})
                cancelled = app.cancel_chat()
                self.assertTrue(cancelled["pending"])
                chat_thread = self._wait_for_chat_reply(app)
            self.assertEqual(chat_thread["messages"][-1]["content"], "Stopped.")

    def test_chat_warns_when_context_is_getting_heavy(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _web_config(directory)
            config["web"]["chat_context_warning_chars"] = 10_000
            config["codex"]["context_window_tokens"] = 3_000
            app = PilferedParrotApp(config, Path(directory))
            answer = "x" * 10_000
            with patch("pilferedparrot.web.capture_dispatch", return_value=RunResult(
                answer, 0, "terra-thread",
            )):
                app.send_chat_message({"content": "Keep tracking this"})
                chat_thread = self._wait_for_chat_reply(app)
            self.assertIn("Start a new Chat", chat_thread["messages"][-1]["content"])
            self.assertIn("practical limit", chat_thread["context_warning"])

    def test_reset_chat_archives_history_that_can_be_opened_from_state(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            with app.store.lock:
                chat_thread = app.store.data["chat"]
                chat_thread["provider_session_id"] = "old-thread"
                chat_thread["messages"] = [{"role": "user", "content": "old"}]
                chat_thread["context_chars"] = 50_000
                app.store.save()
            reset = app.reset_chat()
            self.assertEqual(reset["chat"]["messages"], [])
            self.assertEqual(reset["chat"]["context_chars"], 0)
            self.assertFalse(reset["chat"]["pending"])
            self.assertEqual(app.store.data["chat"]["provider_session_id"], None)
            self.assertEqual(len(reset["chat_history"]), 1)
            archived = reset["chat_history"][0]
            self.assertTrue(archived["archived"])
            self.assertEqual(archived["messages"][0]["content"], "old")
            self.assertNotIn("provider_session_id", archived)

            reloaded = ChatStore(Path(directory) / "chats.json")
            self.assertEqual(
                reloaded.chat_history_public()[0]["messages"][0]["content"], "old",
            )
            self.assertEqual(reloaded.chat_public()["messages"], [])

    def test_chat_model_preference_persists_without_context_metadata(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("pilferedparrot.web.model_max_context_window", return_value=None), \
             patch("pilferedparrot.web.model_context_window", return_value=None):
            config = _web_config(directory)
            app = PilferedParrotApp(config, Path(directory))
            app.reset_chat({"model": "gpt-5.6-luna"})
            restarted = PilferedParrotApp(config, Path(directory))
        self.assertEqual(
            restarted.store.preferences_public()["chat_model"], "gpt-5.6-luna",
        )

    def test_technical_conversations_expose_practical_context_status(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChatStore(Path(directory) / "chats.json", technical_warning_chars=10_000)
            public = store.create(Path(directory), "codex")
            with store.lock:
                store.get(public["id"])["messages"] = [{
                    "role": "user", "content": "x" * 8_000,
                }]
                near_limit = store.public(store.get(public["id"]))
            self.assertEqual(near_limit["context_status"], "near_limit")
            self.assertEqual(near_limit["context_percent"], 80)

    def test_window_capabilities_are_scoped_and_require_the_exact_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat_capability = app.issue_capability("chat")
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.client_address = ("127.0.0.1", 12345)
            handler.headers = {
                "Host": "127.0.0.1:8765",
                "Origin": "http://127.0.0.1:8765",
                "X-PilferedParrot-Capability": app.dashboard_capability,
            }
            self.assertTrue(handler._control_allowed())
            self.assertFalse(handler._control_allowed("chat"))
            handler.headers["X-PilferedParrot-Capability"] = chat_capability
            self.assertTrue(handler._control_allowed("chat"))
            self.assertFalse(handler._control_allowed("dashboard"))
            handler.headers["X-PilferedParrot-Capability"] = "wrong"
            self.assertFalse(handler._control_allowed())
            handler.headers["X-PilferedParrot-Capability"] = app.dashboard_capability
            for origin in (
                "http://127.0.0.1:8766", "http://localhost:8765",
                "https://127.0.0.1:8765", "null",
            ):
                handler.headers["Origin"] = origin
                self.assertFalse(handler._control_allowed(), origin)
            handler.headers.pop("Origin")
            self.assertFalse(handler._control_allowed(), "missing Origin")

    def test_budget_get_accepts_dashboard_capability_without_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)

            def request(headers):
                handler = object.__new__(handler_type)
                handler.path = "/api/budgets"
                handler.client_address = ("127.0.0.1", 12345)
                handler.headers = headers
                handler._json = MagicMock()
                handler.do_GET()
                return handler

            budget = ProviderBudget("codex", True)
            with patch.object(app, "budgets", return_value={"codex": budget}) as budgets:
                handler = request({
                    "Host": "127.0.0.1:8765",
                    "X-PilferedParrot-Capability": app.dashboard_capability,
                })
            budgets.assert_called_once_with()
            handler._json.assert_called_once_with({"codex": budget.as_dict()})

            for capability in (app.issue_capability("chat"), "wrong"):
                handler = request({
                    "Host": "127.0.0.1:8765",
                    "X-PilferedParrot-Capability": capability,
                })
                handler._json.assert_called_once_with(
                    {"error": "dashboard authorization failed"}, HTTPStatus.FORBIDDEN,
                )

            mutation = object.__new__(handler_type)
            mutation.client_address = ("127.0.0.1", 12345)
            mutation.headers = {
                "Host": "127.0.0.1:8765",
                "X-PilferedParrot-Capability": app.dashboard_capability,
            }
            self.assertFalse(mutation._control_allowed(), "mutations still require Origin")

    def test_focused_poll_returns_only_the_requested_work_session(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            first = app.create_chat({"provider": "codex", "cwd": directory})
            app.create_chat({"provider": "codex", "cwd": directory})
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = f"/api/chats/{first['id']}"
            handler.client_address = ("127.0.0.1", 12345)
            handler.headers = {
                "Host": "127.0.0.1:8765",
                "X-PilferedParrot-Capability": app.dashboard_capability,
            }
            handler._json = MagicMock()
            handler.do_GET()
        payload = handler._json.call_args.args[0]
        self.assertEqual(payload["id"], first["id"])
        self.assertNotIn("chats", payload)

    def test_scoped_state_never_returns_a_control_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            dashboard = app.state("dashboard")
            chat = app.state("chat")
        for payload in (dashboard, chat):
            serialized = json.dumps(payload)
            self.assertNotIn("csrf", serialized.lower())
            self.assertNotIn("capability", serialized.lower())
            self.assertNotIn(app.dashboard_capability, serialized)
        self.assertIn("chats", dashboard)
        self.assertNotIn("chat", dashboard)
        self.assertIn("chat", chat)
        self.assertNotIn("chats", chat)

    def test_dashboard_attach_capability_is_stored_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _web_config(directory)
            app = PilferedParrotApp(config, Path(directory))
            origin = "http://127.0.0.1:8765"
            app.persist_dashboard_capability(origin)
            path = Path(config["web"]["chat_store"]).parent / "server-8765.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                _pilferedparrot_dashboard_capability(origin, config),
                app.dashboard_capability,
            )
            app.remove_dashboard_capability()
            self.assertFalse(path.exists())

    def test_handler_snapshots_assets_with_its_api_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "index.html", "app.css", "app.js", "icon.svg",
                "pilferedparrot-icon.png", "company-logo.png", "company-logo-dark.png",
            ):
                (root / name).write_text(f"initial {name}")
            app = PilferedParrotApp(_web_config(directory), root)
            with patch("pilferedparrot.web.ASSET_ROOT", root):
                handler_type = make_handler(app)
            (root / "app.js").write_text("new incompatible JavaScript")
            handler = object.__new__(handler_type)
            handler.send_response = lambda _status: None
            handler.send_header = lambda _name, _value: None
            handler.end_headers = lambda: None
            handler.wfile = io.BytesIO()
            handler._asset("app.js", "text/javascript")
            self.assertEqual(handler.wfile.getvalue(), b"initial app.js")

    def test_delete_conflict_returns_structured_json(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/chats/running"
            handler._control_allowed = lambda _scope="dashboard": True
            handler._request_capability_context = lambda **_kwargs: {
                "scope": "dashboard", "window_id": "main", "provider": "codex",
            }
            handler._json = MagicMock()
            with patch.object(app, "delete_chat", side_effect=ValueError("still running")):
                handler.do_DELETE()
        handler._json.assert_called_once_with(
            {"error": "still running"}, HTTPStatus.CONFLICT,
        )

    def test_handler_serves_separate_work_session_and_chat_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            headers = {}
            handler.send_response = lambda _status: None
            handler.send_header = lambda name, value: headers.__setitem__(name, value)
            handler.end_headers = lambda: None
            handler.wfile = io.BytesIO()
            handler._asset("index.html", "text/html; charset=utf-8")

        body = handler.wfile.getvalue()
        self.assertIn(b'id="newWorkSession"', body)
        self.assertIn(b"New work session", body)
        self.assertNotIn(b'id="resetChat"', body)
        self.assertNotIn(b"New chat", body)
        self.assertNotIn(b'id="newPilferedParrotChat"', body)
        self.assertNotIn(b'id="newChat"', body)
        self.assertNotIn(b"New conversation", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-PilferedParrot-Assets"], ASSET_VERSION)
        self.assertEqual(headers["X-PilferedParrot-Runtime"], RUNTIME_VERSION)

        chat_headers = {}
        handler.wfile = io.BytesIO()
        handler.send_header = lambda name, value: chat_headers.__setitem__(name, value)
        handler._asset("chat.html", "text/html; charset=utf-8")
        chat_body = handler.wfile.getvalue()
        self.assertIn(b'id="resetChat"', chat_body)
        self.assertIn(b"New chat", chat_body)
        self.assertIn(b'id="chatHistoryList"', chat_body)
        self.assertEqual(chat_headers["X-PilferedParrot-Assets"], ASSET_VERSION)

    def test_asset_fingerprint_changes_with_frontend_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("old control")
            original = _asset_fingerprint(root)
            (root / "index.html").write_text("new Parrot chat")
            self.assertNotEqual(_asset_fingerprint(root), original)

    def test_runtime_fingerprint_changes_with_backend_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "web.py").write_text("old runtime")
            original = _runtime_fingerprint(root)
            (root / "web.py").write_text("new runtime")
            self.assertNotEqual(_runtime_fingerprint(root), original)

    def test_listener_is_stale_when_runtime_or_frontend_assets_do_not_match(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Server": "PilferedParrot/test"}

        def status(payload):
            response.read.return_value = json.dumps(payload).encode("utf-8")
            with patch("pilferedparrot.web.urlopen", return_value=response):
                return _pilferedparrot_status("http://127.0.0.1:8765")

        base = {"service": "pilferedparrot", "api_generation": API_GENERATION}
        self.assertEqual(status(base), "stale")
        self.assertEqual(status({**base, "asset_version": "old-assets"}), "stale")
        current_assets = {**base, "asset_version": ASSET_VERSION}
        self.assertEqual(status(current_assets), "stale")
        self.assertEqual(status({**current_assets, "runtime_version": "old-runtime"}), "stale")
        self.assertEqual(
            status({**current_assets, "runtime_version": RUNTIME_VERSION}), "compatible",
        )

    def test_listener_recognizes_pre_capability_api_as_stale(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Server": "PilferedParrot/old"}
        response.read.return_value = json.dumps({"chats": [], "csrf_token": "old"}).encode()
        missing = HTTPError(
            "http://127.0.0.1:8765/api/status", HTTPStatus.NOT_FOUND,
            "not found", {}, None,
        )
        with patch("pilferedparrot.web.urlopen", side_effect=[missing, response]):
            self.assertEqual(_pilferedparrot_status("http://127.0.0.1:8765"), "stale")

    def test_launcher_close_notifies_only_its_exact_server_instance(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = HTTPStatus.ACCEPTED
        url = _browser_url("http://127.0.0.1:8765", "exact-instance-token")
        with patch("pilferedparrot.web.urlopen", return_value=response) as open_url:
            self.assertTrue(_notify_window_closed(url))
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/window/close")
        self.assertEqual(
            request.get_header("X-pilferedparrot-capability"), "exact-instance-token",
        )
        self.assertEqual(request.get_header("Origin"), "http://127.0.0.1:8765")
        self.assertNotIn("capability", request.full_url)
        self.assertEqual(request.data, b'{"window_id":"main"}')

    def test_shutdown_route_stops_the_serving_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/shutdown"
            handler.server = MagicMock()
            handler._control_allowed = lambda _scope="dashboard": True
            handler._request_capability_context = lambda **_kwargs: {
                "scope": "dashboard", "window_id": "main", "provider": "codex",
            }
            handler._read_json = lambda: {}
            handler._json = MagicMock()
            with patch("pilferedparrot.web.threading.Thread") as thread:
                handler.do_POST()
            handler._json.assert_called_once_with({"ok": True}, HTTPStatus.ACCEPTED)
            self.assertIs(thread.call_args.kwargs["target"], handler.server.shutdown)
            thread.return_value.start.assert_called_once_with()

    def test_main_window_reload_cancels_deferred_close(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.server = MagicMock()
            handler._control_allowed = lambda _scope="dashboard": True
            handler._request_capability_context = lambda **_kwargs: {
                "scope": "dashboard", "window_id": "main", "provider": "codex",
            }
            handler._read_json = lambda: {}
            handler._json = MagicMock()
            timer = MagicMock()
            with patch("pilferedparrot.web.threading.Timer", return_value=timer) as factory:
                handler.path = "/api/window/close"
                handler.do_POST()
                handler.path = "/api/window/open"
                handler.do_POST()
            factory.assert_called_once_with(2, handler.server.shutdown)
            timer.start.assert_called_once_with()
            timer.cancel.assert_called_once_with()

    def test_two_main_documents_are_counted_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.server = MagicMock()
            handler._control_allowed = lambda _scope="dashboard": True
            handler._request_capability_context = lambda **_kwargs: {
                "scope": "dashboard", "window_id": "main", "provider": "codex",
            }
            handler._json = MagicMock()
            payload = {"document_id": "document-one"}
            handler._read_json = lambda: payload
            with patch("pilferedparrot.web.threading.Timer") as timer:
                handler.path = "/api/window/open"
                handler.do_POST()
                payload["document_id"] = "document-two"
                handler.do_POST()
                handler.path = "/api/window/close"
                payload["document_id"] = "document-one"
                handler.do_POST()
                timer.assert_not_called()
                payload["document_id"] = "document-two"
                handler.do_POST()
            timer.assert_called_once_with(2, handler.server.shutdown)

    def test_server_stays_running_until_last_provider_window_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.server = MagicMock()
            handler._control_allowed = lambda _scope="dashboard": True
            capability_context = {
                "scope": "dashboard", "window_id": "main", "provider": "codex",
            }
            handler._request_capability_context = lambda **_kwargs: capability_context
            handler._json = MagicMock()
            timer = MagicMock()
            payload = {}
            handler._read_json = lambda: payload
            with patch("pilferedparrot.web.threading.Timer", return_value=timer) as factory:
                handler.path = "/api/window/open"
                handler.do_POST()
                capability_context["window_id"] = "claude-window"
                capability_context["provider"] = "claude"
                handler.do_POST()
                handler.path = "/api/window/close"
                capability_context["window_id"] = "main"
                capability_context["provider"] = "codex"
                handler.do_POST()
                factory.assert_not_called()
                capability_context["window_id"] = "claude-window"
                capability_context["provider"] = "claude"
                handler.do_POST()
            factory.assert_called_once_with(2, handler.server.shutdown)
            timer.start.assert_called_once_with()

    @patch("pilferedparrot.web.threading.Thread")
    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web.tempfile.mkdtemp", return_value="/tmp/pilferedparrot-chat-test")
    @patch("pilferedparrot.web.shutil.which")
    def test_chat_window_uses_an_isolated_normal_browser_profile(
        self, which, _mkdtemp, popen, thread,
    ):
        which.side_effect = lambda command: (
            "/usr/bin/google-chrome-stable" if command == "google-chrome-stable" else None
        )
        process = popen.return_value
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            result = app.open_chat_window("http://127.0.0.1:8765/chat", {
                "width": 960, "height": 540, "left": 900, "top": 28,
            })
        self.assertEqual(result, {"ok": True, "existing": False})
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/google-chrome-stable")
        self.assertIn("--user-data-dir=/tmp/pilferedparrot-chat-test", command)
        self.assertIn("--window-size=960,540", command)
        self.assertIn("--window-position=900,28", command)
        self.assertIn("--class=pilferedparrot-chat", command)
        self.assertTrue(any(
            value.startswith("--app=http://127.0.0.1:8765/chat#capability=")
            for value in command
        ))
        self.assertIn("--start-maximized", command)
        thread.return_value.start.assert_called_once_with()

    @patch("pilferedparrot.web.threading.Thread")
    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web.tempfile.mkdtemp", return_value="/tmp/pilferedparrot-chat-inherited")
    @patch("pilferedparrot.web.shutil.which")
    def test_chat_window_capability_preserves_spawned_provider_and_model(
        self, which, _mkdtemp, popen, _thread,
    ):
        """A Chat window capability carries the source work window selection."""
        which.side_effect = lambda command: (
            "/usr/bin/google-chrome-stable" if command == "google-chrome-stable" else None
        )
        process = popen.return_value
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            app.open_chat_window("http://127.0.0.1:8765/chat", {
                "provider": "claude", "model": "claude-sonnet-5",
                "width": 960, "height": 540, "left": 0, "top": 0,
            })
            token = app.chat_window_capability
            self.assertIsNotNone(token)
            context = app.capability_context(token)
        self.assertEqual(context["scope"], "chat")
        self.assertEqual(context["provider"], "claude")
        self.assertEqual(context["model"], "claude-sonnet-5")

    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web.shutil.which")
    def test_theme_gallery_uses_the_persistent_pilferedparrot_profile(self, which, popen):
        which.side_effect = lambda command: (
            "/usr/bin/google-chrome-stable" if command == "google-chrome-stable" else None
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
                app = PilferedParrotApp(_web_config(directory), Path(directory))
                result = app.open_chrome_theme_gallery()
            expected_profile = Path(directory) / "pilferedparrot/chrome-profile"
            self.assertTrue(expected_profile.is_dir())
        self.assertEqual(result, {"ok": True})
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/google-chrome-stable")
        self.assertIn(f"--user-data-dir={expected_profile}", command)
        self.assertIn("--new-window", command)
        self.assertIn("--start-maximized", command)
        self.assertEqual(command[-1], CHROME_THEME_GALLERY_URL)
        self.assertNotIn("--app", command)

    def test_installed_chrome_theme_is_safely_exposed_to_the_app(self):
        theme_id = "abcdefghijklmnopabcdefghijklmnop"
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            profile = state_root / "pilferedparrot/chrome-profile/Default"
            pack = profile / "Extensions" / theme_id / "1.0_0"
            locale = pack / "_locales/en"
            images = pack / "images"
            locale.mkdir(parents=True)
            images.mkdir()
            (locale / "messages.json").write_text(json.dumps({
                "extName": {"message": "Forest test"},
            }))
            (images / "background.png").write_bytes(b"\x89PNG\r\n\x1a\nbackground")
            (pack / "manifest.json").write_text(json.dumps({
                "name": "__MSG_extName__", "default_locale": "en", "version": "1.0",
                "theme": {
                    "colors": {"frame": [71, 105, 91], "toolbar": [207, 221, 192]},
                    "images": {"theme_ntp_background": "images/background.png"},
                    "properties": {
                        "ntp_background_alignment": "bottom",
                        "ntp_background_repeat": "repeat-x",
                    },
                },
            }))
            profile.mkdir(parents=True, exist_ok=True)
            (profile / "Preferences").write_text(json.dumps({
                "extensions": {"theme": {"id": theme_id, "pack": str(pack)}},
            }))
            with patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
                public, background = _selected_chrome_theme()
                app = PilferedParrotApp(_web_config(directory), state_root)
                asset = app.chrome_theme_background()
            self.assertEqual(public["name"], "Forest test")
            self.assertEqual(public["colors"]["frame"], "#47695b")
            self.assertEqual(public["background_alignment"], "bottom")
            self.assertEqual(background, images / "background.png")
            self.assertEqual(asset, (b"\x89PNG\r\n\x1a\nbackground", "image/png"))

    def test_chrome_theme_background_cannot_escape_its_extension_pack(self):
        theme_id = "abcdefghijklmnopabcdefghijklmnop"
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            profile = state_root / "pilferedparrot/chrome-profile/Default"
            pack = profile / "Extensions" / theme_id / "1.0_0"
            pack.mkdir(parents=True)
            outside = profile / "outside.png"
            outside.write_bytes(b"not exposed")
            (pack / "manifest.json").write_text(json.dumps({
                "name": "Unsafe test", "version": "1",
                "theme": {"images": {"theme_ntp_background": "../../../outside.png"}},
            }))
            (profile / "Preferences").write_text(json.dumps({
                "extensions": {"theme": {"id": theme_id, "pack": str(pack)}},
            }))
            with patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
                public, background = _selected_chrome_theme()
            self.assertTrue(public["active"])
            self.assertFalse(public["background"])
            self.assertIsNone(background)

    @patch("pilferedparrot.web.shutil.which", return_value=None)
    def test_theme_gallery_requires_a_chrome_family_browser(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            with self.assertRaisesRegex(RuntimeError, "Chrome or Chromium is required"):
                app.open_chrome_theme_gallery()

    def test_chat_window_route_uses_the_current_local_server(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/chat/window"
            handler.headers = {"Host": "127.0.0.1:8765"}
            handler._control_allowed = lambda _scope="dashboard": True
            handler._request_capability_context = lambda **_kwargs: {
                "scope": "dashboard", "window_id": "main", "provider": "codex",
            }
            payload = {"width": 960, "height": 540, "left": 900, "top": 28}
            handler._read_json = lambda: payload
            handler._json = MagicMock()
            with patch.object(app, "open_chat_window", return_value={"ok": True}) as launch:
                handler.do_POST()
        launch.assert_called_once_with(
            "http://127.0.0.1:8765/chat", {**payload, "provider": "codex"},
        )
        handler._json.assert_called_once_with({"ok": True})

    @patch("pilferedparrot.web.threading.Thread")
    @patch("pilferedparrot.web.resolve_command")
    @patch("pilferedparrot.web.subprocess.Popen")
    def test_provider_login_launches_official_cli_flow_in_default_browser(
        self, popen, resolve, thread,
    ):
        resolve.side_effect = lambda _config, provider: f"/usr/bin/{provider}"
        codex_process = MagicMock()
        claude_process = MagicMock()
        codex_process.poll.return_value = None
        claude_process.poll.return_value = None
        popen.side_effect = [codex_process, claude_process]
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            with patch.dict(os.environ, {"BROWSER": "pilferedparrot-app-browser"}):
                self.assertEqual(
                    app.provider_auth_action("codex", "login"),
                    {
                        "ok": True, "launched": True, "active": True,
                        "destination": "browser", "confirmation_code": False,
                    },
                )
                self.assertEqual(
                    app.provider_auth_action("claude", "login"),
                    {
                        "ok": True, "launched": True, "active": True,
                        "destination": "browser", "confirmation_code": True,
                    },
                )
        self.assertEqual(
            [call.args[0] for call in popen.call_args_list],
            [["/usr/bin/codex", "login"], ["/usr/bin/claude", "auth", "login"]],
        )
        for call in popen.call_args_list:
            self.assertNotIn("BROWSER", call.kwargs["env"])
        claude_process.stdin.write.assert_called_once_with(b"\n")
        claude_process.stdin.flush.assert_called_once_with()
        claude_process.stdin.close.assert_not_called()
        codex_process.stdin.close.assert_called_once_with()
        self.assertEqual(thread.call_count, 2)

    @patch("pilferedparrot.web.threading.Thread")
    @patch("pilferedparrot.web.time.sleep")
    @patch("pilferedparrot.web.resolve_command", return_value="/usr/bin/claude")
    @patch("pilferedparrot.web.subprocess.Popen")
    def test_provider_login_accepts_anthropic_code_in_the_original_process(
        self, popen, _resolve, _sleep, _thread,
    ):
        process = MagicMock()
        process.poll.return_value = None
        popen.return_value = process
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            app.provider_auth_action("claude", "login")
            self.assertEqual(
                app.submit_provider_auth_code(
                    "claude", {"code": "browser-code#oauth-state"},
                ),
                {"ok": True, "submitted": True},
            )
            self.assertEqual(
                app.provider_auth_action("claude", "login")["launched"], False,
            )
        self.assertEqual(
            process.stdin.write.call_args_list,
            [call(b"\n"), call(b"browser-code#oauth-state\n")],
        )
        self.assertEqual(process.stdin.flush.call_count, 2)
        self.assertEqual(popen.call_count, 1)

    def test_provider_login_rejects_invalid_or_stale_anthropic_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            with self.assertRaisesRegex(ValueError, "including #"):
                app.submit_provider_auth_code("claude", {"code": "incomplete"})
            with self.assertRaisesRegex(ValueError, "no longer waiting"):
                app.submit_provider_auth_code("claude", {"code": "code#state"})

    @patch("pilferedparrot.web.time.sleep")
    @patch("pilferedparrot.web.resolve_command", return_value="/usr/bin/claude")
    @patch("pilferedparrot.web.subprocess.Popen")
    def test_provider_login_surfaces_immediate_cli_failure(
        self, popen, _resolve, _sleep,
    ):
        process = MagicMock()
        process.poll.return_value = 1
        process.stderr.read.return_value = (
            b"Login failed: browser error at https://example.test/private-token\n"
        )
        popen.return_value = process
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            with self.assertRaisesRegex(
                RuntimeError,
                r"Login failed: browser error at \[sign-in URL omitted\]",
            ):
                app.provider_auth_action("claude", "login")
        process.stdin.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

    @patch("pilferedparrot.web.resolve_command", return_value="/usr/bin/claude")
    @patch("pilferedparrot.web.subprocess.run")
    def test_provider_logout_uses_noninteractive_cli_after_ui_confirmation(
        self, run, _resolve,
    ):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            result = app.provider_auth_action("claude", "logout")
        self.assertEqual(result, {"ok": True, "launched": False})
        self.assertEqual(run.call_args.args[0], ["/usr/bin/claude", "auth", "logout"])

    def test_shutdown_terminates_an_active_provider_login(self):
        process = MagicMock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            app.provider_logins["claude"] = MagicMock(process=process)
            app.shutdown()
        process.stdin.close.assert_called_once_with()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once()
        self.assertEqual(app.provider_logins, {})

    def test_provider_auth_route_passes_only_validated_provider_and_action(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/providers/claude/login"
            handler._control_allowed = lambda _scope="dashboard": True
            handler._request_capability_context = lambda **_kwargs: {
                "scope": "dashboard", "window_id": "main", "provider": "codex",
            }
            handler._read_json = lambda: {}
            handler._json = MagicMock()
            with patch.object(
                app, "provider_auth_action", return_value={"ok": True},
            ) as action:
                handler.do_POST()
        action.assert_called_once_with("claude", "login")
        handler._json.assert_called_once_with({"ok": True})

    def test_provider_auth_code_route_submits_only_to_the_active_claude_login(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/providers/claude/code"
            handler._control_allowed = lambda _scope="dashboard": True
            handler._request_capability_context = lambda **_kwargs: {
                "scope": "dashboard", "window_id": "main", "provider": "codex",
            }
            payload = {"code": "browser-code#oauth-state"}
            handler._read_json = lambda: payload
            handler._json = MagicMock()
            with patch.object(
                app, "submit_provider_auth_code", return_value={"ok": True},
            ) as submit:
                handler.do_POST()
        submit.assert_called_once_with("claude", payload)
        handler._json.assert_called_once_with({"ok": True})

    def test_provider_preference_route_supports_dashboard_and_scoped_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/preferences/provider"
            handler._control_allowed = lambda _scope="dashboard": True
            handler._read_json = lambda: {"provider": "claude", "model": "opus"}
            handler._json = MagicMock()
            with patch.object(app, "set_provider_preferences", return_value={}) as save:
                handler._request_capability_context = lambda **_kwargs: {
                    "scope": "dashboard", "window_id": "main", "provider": "codex",
                }
                handler.do_POST()
                save.assert_called_once_with(
                    {"provider": "claude", "model": "opus"}, window_provider=None,
                )

                save.reset_mock()
                handler._request_capability_context = lambda **_kwargs: {
                    "scope": "dashboard", "window_id": "isolated", "provider": "claude",
                }
                handler.do_POST()
                save.assert_called_once_with(
                    {"provider": "claude", "model": "opus"}, window_provider="claude",
                )

    @patch("pilferedparrot.web.threading.Thread")
    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web._chromium_browser", return_value="/usr/bin/chromium")
    def test_provider_window_has_isolated_profile_capability_and_selection(
        self, _browser, popen, thread,
    ):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            result = app.open_provider_window("http://127.0.0.1:8765/", {
                "provider": "claude", "model": "opus", "width": 1000, "height": 700,
                "left": 40, "top": 30,
            })
            record = app.provider_windows[result["launch_id"]]
            self.assertEqual(record["provider"], "claude")
            self.assertEqual(record["model"], "opus")
            self.assertEqual(app.capability_scope(record["capability"]), "dashboard")
            capability = app.capability_context(record["capability"])
            self.assertEqual(capability["window_id"], result["launch_id"])
            self.assertEqual(capability["history_id"], "provider-claude")
            app.shutdown()
        command = popen.call_args.args[0]
        self.assertTrue(any(value.startswith("--user-data-dir=/tmp/pilferedparrot-claude-") for value in command))
        app_argument = next(value for value in command if value.startswith("--app="))
        self.assertIn(f"#capability={record['capability']}", app_argument)
        self.assertIn("provider=claude", app_argument)
        self.assertIn("model=opus", app_argument)
        self.assertIn(f"window={result['window_id']}", app_argument)
        self.assertEqual(result["window_id"], "provider-claude")
        self.assertIn("--start-maximized", command)
        thread.return_value.start.assert_called_once_with()

    @patch("pilferedparrot.web.threading.Thread")
    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web._chromium_browser", return_value="/usr/bin/chromium")
    def test_provider_window_passes_validated_source_project_to_child_window(
        self, _browser, popen, _thread,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            app = PilferedParrotApp(_web_config(directory), root)
            result = app.open_provider_window("http://127.0.0.1:8765/", {
                "provider": "qwen", "cwd": str(project), "width": 1000,
                "height": 700, "left": 40, "top": 30,
            })
            app.shutdown()
        app_argument = next(value for value in popen.call_args.args[0] if value.startswith("--app="))
        self.assertIn("cwd=%2F", app_argument)
        self.assertIn("project", app_argument)

    def test_theme_gallery_route_opens_the_persistent_browser_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/browser/theme"
            handler._control_allowed = lambda _scope="dashboard": True
            handler._request_capability_context = lambda **_kwargs: {
                "scope": "dashboard", "window_id": "main", "provider": "codex",
            }
            handler._read_json = lambda: {}
            handler._json = MagicMock()
            with patch.object(
                app, "open_chrome_theme_gallery", return_value={"ok": True},
            ) as launch:
                handler.do_POST()
        launch.assert_called_once_with()
        handler._json.assert_called_once_with({"ok": True})

    def test_theme_status_route_reports_the_selected_browser_theme(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/browser/theme"
            handler.headers = {}
            handler._local_request_allowed = lambda: True
            handler._request_capability_scope = lambda: "dashboard"
            handler._json = MagicMock()
            selected = {"active": True, "name": "Forest test"}
            with patch.object(app, "browser_theme", return_value=selected) as status:
                handler.do_GET()
        status.assert_called_once_with()
        handler._json.assert_called_once_with(selected)

    def test_theme_background_requires_a_window_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/browser/theme/background"
            handler._local_request_allowed = lambda: True
            handler._request_capability_scope = lambda: None
            handler._json = MagicMock()
            with patch.object(app, "chrome_theme_background") as background:
                handler.do_GET()
        background.assert_not_called()
        handler._json.assert_called_once_with(
            {"error": "window authorization failed"}, HTTPStatus.FORBIDDEN,
        )

    @patch("pilferedparrot.web._pilferedparrot_dashboard_capability", return_value="dashboard-token")
    @patch("pilferedparrot.web.webbrowser.open")
    @patch("pilferedparrot.web._pilferedparrot_status", return_value="compatible")
    def test_gui_launch_opens_existing_pilferedparrot(
        self, running, open_browser, capability,
    ):
        with tempfile.TemporaryDirectory() as directory:
            config = _web_config(directory)
            self.assertEqual(serve(config, Path(directory)), 0)
        running.assert_called_once()
        open_browser.assert_called_once_with(
            f"http://127.0.0.1:8765/?generation={API_GENERATION}&assets={ASSET_VERSION}"
            f"&runtime={RUNTIME_VERSION}#capability=dashboard-token",
        )
        capability.assert_called_once_with("http://127.0.0.1:8765", config)

    @patch("pilferedparrot.web._terminate_stale_pilferedparrot")
    @patch("pilferedparrot.web._pilferedparrot_status", return_value="stale")
    @patch("pilferedparrot.web.ThreadingHTTPServer")
    def test_gui_replaces_stale_pilferedparrot_after_generation_check(
        self, server_factory, status, terminate,
    ):
        server = server_factory.return_value
        server.serve_forever.side_effect = KeyboardInterrupt
        with tempfile.TemporaryDirectory() as directory:
            config = _web_config(directory)
            self.assertEqual(serve(config, Path(directory), open_browser=False), 0)
        terminate.assert_called_once_with(
            "http://127.0.0.1:8765", config["web"]["port"],
        )
        server_factory.assert_called_once()

    @patch("pilferedparrot.web._pilferedparrot_status", return_value="unavailable")
    @patch("pilferedparrot.web.subprocess.run")
    @patch("pilferedparrot.web.shutil.which", return_value="/usr/bin/fuser")
    def test_stale_replacement_signals_only_the_exact_listener(
        self, _which, run, _status,
    ):
        _terminate_stale_pilferedparrot("http://localhost:8765", 8765)
        run.assert_called_once_with(
            ["/usr/bin/fuser", "-k", "-INT", "8765/tcp"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_direct_provider_runs_without_budget_or_router_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            with patch.object(app, "budgets") as budgets, \
                 patch("pilferedparrot.web.capture_dispatch",
                       return_value=RunResult("done", 0, "codex-thread")) as dispatch:
                app.send_message(chat["id"], {"content": "fix it", "provider": "codex"})
                updated = self._wait_for_chat(app, chat["id"])
            budgets.assert_not_called()
            self.assertEqual(dispatch.call_args.args[0], "codex")
            self.assertEqual(updated["messages"][-1]["content"], "done")

    def test_first_turn_rejects_unapproved_project_mismatch_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            other = root / "game"
            selected.mkdir()
            other.mkdir()
            (other / ".git").mkdir()
            config = _web_config(directory)
            config["codex"]["additional_write_dirs"] = []
            app = PilferedParrotApp(config, selected)
            chat = app.create_chat({"provider": "codex", "cwd": selected})
            with patch("pilferedparrot.web.capture_dispatch") as dispatch, \
                 self.assertRaisesRegex(ValueError, "project mismatch"):
                app.send_message(chat["id"], {
                    "content": f"Implement the fix in {other}.", "provider": "codex",
                })
            dispatch.assert_not_called()
            self.assertEqual(app.store.get(chat["id"])["messages"], [])

    def test_qwen_home_workspace_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            config = _web_config(directory)
            with patch("pilferedparrot.web.Path.home", return_value=home):
                app = PilferedParrotApp(config, home)
                with self.assertRaisesRegex(ValueError, "allow_home_workspace"):
                    app.create_chat({"provider": "qwen", "cwd": str(home)})
                config["qwen"]["allow_home_workspace"] = True
                allowed = PilferedParrotApp(config, home).create_chat({
                    "provider": "qwen", "cwd": str(home),
                })
            self.assertEqual(allowed["cwd"], str(home.resolve()))

    def test_qwen_workspace_cannot_be_a_parent_of_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            config = _web_config(directory)
            config["qwen"]["allow_home_workspace"] = True
            with patch("pilferedparrot.web.Path.home", return_value=home):
                app = PilferedParrotApp(config, root)
                with self.assertRaisesRegex(ValueError, "parent of the home directory"):
                    app.create_chat({"provider": "qwen", "cwd": str(root)})

    def test_configured_codex_write_root_allows_external_project_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            other = root / "game"
            selected.mkdir()
            other.mkdir()
            config = _web_config(directory)
            config["codex"]["additional_write_dirs"] = [str(other)]
            app = PilferedParrotApp(config, selected)
            chat = app.create_chat({"provider": "codex", "cwd": selected})
            with patch("pilferedparrot.web.capture_dispatch",
                       return_value=RunResult("done", 0, "thread")) as dispatch:
                app.send_message(chat["id"], {
                    "content": f"Implement the fix in {other}.", "provider": "codex",
                })
                self._wait_for_chat(app, chat["id"])
            dispatch.assert_called_once()

    def test_provider_progress_is_visible_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})

            def stream(_provider, _prompt, _cwd, _conversation, _config, cancel_event):
                report = getattr(cancel_event, "_pilferedparrot_progress")
                for index in range(110):
                    report("tool", f"command {index}")
                return RunResult("Finished.", 0, "codex-thread")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=stream):
                app.send_message(chat["id"], {"content": "show work", "provider": "codex"})
                updated = self._wait_for_chat(app, chat["id"])
            activity = updated["messages"][-1]["activity"]
            self.assertEqual(len(activity), 100)
            self.assertEqual(activity[0]["content"], "command 10")

    def test_switching_provider_does_not_resume_other_provider_session(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            resumed = []

            def dispatch(_provider, _prompt, _cwd, conversation, _config, _cancel):
                resumed.append(conversation.provider_session_id)
                return RunResult("done", 0, f"session-{len(resumed)}")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch), \
                 patch("pilferedparrot.web.ensure_qwen"):
                app.send_message(chat["id"], {"content": "first", "provider": "codex"})
                self._wait_for_chat(app, chat["id"])
                app.send_message(chat["id"], {"content": "second", "provider": "qwen"})
                self._wait_for_chat(app, chat["id"])
            self.assertEqual(resumed, [None, None])

    def test_changing_models_starts_fresh_provider_session(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            resumed = []

            def dispatch(_provider, _prompt, _cwd, conversation, _config, _cancel):
                resumed.append(conversation.provider_session_id)
                return RunResult("done", 0, f"session-{len(resumed)}")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch):
                app.send_message(chat["id"], {
                    "content": "first", "provider": "codex", "model": "gpt-a",
                })
                self._wait_for_chat(app, chat["id"])
                app.send_message(chat["id"], {
                    "content": "second", "provider": "codex", "model": "gpt-b",
                })
                self._wait_for_chat(app, chat["id"])
            self.assertEqual(resumed, [None, None])

    def test_restart_recovers_persisted_pending_response(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            with app.store.lock:
                stored = app.store.get(chat["id"])
                stored["messages"] = [{"id": "pending", "role": "assistant", "pending": True}]
                app.store.save()
            self.assertEqual(app.recover_interrupted(), 1)
            self.assertTrue(app.store.list_public()[0]["messages"][0]["interrupted"])


class ClaudeDispatchTests(unittest.TestCase):
    @patch("pilferedparrot.dispatch.resolve_command", return_value="/usr/bin/claude")
    def test_claude_command_uses_print_stream_json_and_resume(self, _resolve):
        config = load_config()
        config["claude"]["command"] = "/usr/bin/claude"
        config["claude"]["model"] = "sonnet"
        command = _claude_command(Conversation(provider_session_id="session-1"), config)
        self.assertEqual(command[:5], ["/usr/bin/claude", "-p", "--output-format", "stream-json", "--verbose"])
        self.assertIn("--resume", command)
        self.assertIn("session-1", command)
        self.assertEqual(command[-2:], ["--model", "sonnet"])

    @patch("pilferedparrot.dispatch.resolve_command", return_value="/usr/bin/claude")
    def test_claude_command_preserves_exact_versioned_model_id(self, _resolve):
        config = load_config()
        config["claude"]["command"] = "/usr/bin/claude"
        config["claude"]["model"] = "claude-fable-5"
        command = _claude_command(Conversation(), config)
        self.assertEqual(command[-2:], ["--model", "claude-fable-5"])

    @patch("pilferedparrot.dispatch.resolve_command", return_value="/usr/bin/claude")
    @patch("pilferedparrot.dispatch._stream_process")
    def test_capture_claude_preserves_session_and_final_result(self, stream, _resolve):
        stream.side_effect = lambda command, prompt, cwd, **kwargs: (
            kwargs["stdout_line"]('{"type":"system","session_id":"session-2"}'),
            kwargs["stdout_line"](
                '{"type":"result","result":"Done","usage":'
                '{"input_tokens":120,"output_tokens":8}}'
            ),
            subprocess.CompletedProcess(command, 0, "", ""),
        )[-1]
        config = load_config()
        conversation = Conversation()
        result = capture_claude("hello", Path.cwd(), conversation, config)
        self.assertEqual(result.text, "Done")
        self.assertEqual(result.session_id, "session-2")
        self.assertEqual((result.input_tokens, result.output_tokens), (120, 8))
        self.assertEqual(conversation.provider_session_id, "session-2")
        self.assertNotIn("hello", stream.call_args.args[0])
        self.assertEqual(stream.call_args.args[1], "hello")


class DispatchCancellationTests(unittest.TestCase):
    def test_compatible_http_request_can_be_cancelled_while_transport_is_stalled(self):
        cancel = threading.Event()
        release = threading.Event()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"choices":[{"message":{"content":"late"}}]}'

        def stalled(*_args, **_kwargs):
            release.wait(5)
            return response

        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        try:
            with patch("pilferedparrot.qwen.open_compatible_url", side_effect=stalled), \
                 self.assertRaises(RunCancelled):
                _chat_completion([], load_config(), cancel_event=cancel)
        finally:
            release.set()
            timer.cancel()

    def test_capture_process_can_be_cancelled(self):
        cancel = threading.Event()
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        try:
            with self.assertRaises(RunCancelled):
                _capture_process(
                    ["/bin/sh", "-c", "sleep 30"], "", Path.cwd(),
                    cancel_event=cancel, timeout_seconds=5,
                )
        finally:
            timer.cancel()

    def test_stream_timeout_applies_while_child_is_not_reading_large_prompt(self):
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "timed out"):
            _stream_process(
                ["/bin/sh", "-c", "sleep 30"], "x" * 2_000_000, Path.cwd(),
                cancel_event=None, timeout_seconds=0.2, stdout_line=lambda _line: None,
            )
        self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
