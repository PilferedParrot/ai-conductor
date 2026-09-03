import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pilferedparrot.config import load_config
from pilferedparrot.web import API_GENERATION, ASSET_VERSION, RUNTIME_VERSION
from pilferedparrot.web import (
    PilferedParrotApp, _IPv6ThreadingHTTPServer, _select_project_directory,
    make_handler, serve,
)


class ProviderLaunchWorkspaceTests(unittest.TestCase):
    """A provider window must open even when it cannot inherit its parent's folder."""

    def _app(self, config, default_cwd):
        # Only the workspace-selection attributes matter here; a full app would
        # start capability and model machinery this behaviour does not touch.
        app = object.__new__(PilferedParrotApp)
        app.config = config
        app.default_cwd = Path(default_cwd)
        app.renamed_repository_root = None
        return app

    def test_inherited_home_falls_back_to_the_configured_default(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["qwen"]["default_workspace"] = directory
            app = self._app(config, Path.home())
            self.assertEqual(
                app._provider_launch_workspace("qwen", None), Path(directory).resolve(),
            )

    def test_launch_asks_for_a_folder_when_nothing_is_usable(self):
        config = load_config()
        config["qwen"]["default_workspace"] = None
        app = self._app(config, Path.home())
        self.assertIsNone(app._provider_launch_workspace("qwen", None))

    def test_unusable_default_workspace_asks_rather_than_failing(self):
        config = load_config()
        config["qwen"]["default_workspace"] = "/nonexistent-pilferedparrot-workspace"
        app = self._app(config, Path.home())
        self.assertIsNone(app._provider_launch_workspace("qwen", None))

    def test_path_resolution_error_falls_back_to_the_configured_default(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["qwen"]["default_workspace"] = directory
            app = self._app(config, Path.home())
            with patch(
                "pilferedparrot.web._migrate_renamed_project_path",
                side_effect=[RuntimeError("symlink loop"), Path(directory)],
            ):
                self.assertEqual(
                    app._provider_launch_workspace("qwen", None), Path(directory).resolve(),
                )

    def test_codex_still_inherits_the_home_directory(self):
        config = load_config()
        app = self._app(config, Path.home())
        self.assertEqual(
            app._provider_launch_workspace("codex", None), Path.home().resolve(),
        )


class ProjectDirectoryChooserTests(unittest.TestCase):
    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web.shutil.which")
    def test_native_chooser_returns_a_validated_directory(self, which, popen):
        which.side_effect = lambda name: "/usr/bin/zenity" if name == "zenity" else None
        with tempfile.TemporaryDirectory() as directory:
            process = MagicMock(pid=321, returncode=0)
            process.communicate.return_value = (f"{directory}\n", "")
            popen.return_value = process
            self.assertEqual(_select_project_directory(None), Path(directory).resolve())
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/zenity")
        self.assertIn("--directory", command)
        self.assertIn("--modal", command)
        self.assertNotIn("check", popen.call_args.kwargs)

    @patch("pilferedparrot.web.subprocess.run")
    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web.shutil.which")
    def test_native_chooser_is_attached_and_raised(self, which, popen, run):
        which.side_effect = lambda name: {
            "xdotool": "/usr/bin/xdotool", "zenity": "/usr/bin/zenity",
        }.get(name)
        process = MagicMock(pid=991, returncode=1)
        process.communicate.return_value = ("", "")
        popen.return_value = process
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="73400324\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="800\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]

        self.assertIsNone(_select_project_directory(None))

        self.assertEqual(run.call_args_list[0].args[0], [
            "/usr/bin/xdotool", "getactivewindow",
        ])
        chooser = popen.call_args.args[0]
        self.assertIn("--modal", chooser)
        self.assertIn("--attach=73400324", chooser)
        self.assertEqual(run.call_args_list[1].args[0], [
            "/usr/bin/xdotool", "search", "--sync", "--all", "--pid", "991",
            "--name", "^Choose\\ project\\ folder$",
        ])
        self.assertEqual(run.call_args_list[2].args[0], [
            "/usr/bin/xdotool", "windowactivate", "--sync", "800",
        ])
        self.assertEqual(run.call_args_list[3].args[0], [
            "/usr/bin/xdotool", "windowraise", "800",
        ])

    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web.shutil.which")
    def test_cancelling_native_chooser_is_not_an_error(self, which, popen):
        which.side_effect = lambda name: "/usr/bin/zenity" if name == "zenity" else None
        process = MagicMock(pid=321, returncode=1)
        process.communicate.return_value = ("", "")
        popen.return_value = process
        self.assertIsNone(_select_project_directory(None))

    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web.shutil.which")
    def test_chooser_still_works_when_xdotool_cannot_find_window(self, which, popen):
        which.side_effect = lambda name: {
            "xdotool": "/usr/bin/xdotool", "zenity": "/usr/bin/zenity",
        }.get(name)
        process = MagicMock(pid=321, returncode=0)
        process.communicate.return_value = (str(Path.home()) + "\n", "")
        popen.return_value = process
        with patch("pilferedparrot.web.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 1, stdout="", stderr="")):
            self.assertEqual(_select_project_directory(None), Path.home().resolve())

    @patch("pilferedparrot.web.subprocess.run")
    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web.shutil.which")
    def test_chooser_is_raised_even_when_focus_activation_times_out(self, which, popen, run):
        which.side_effect = lambda name: {
            "xdotool": "/usr/bin/xdotool", "zenity": "/usr/bin/zenity",
        }.get(name)
        process = MagicMock(pid=991, returncode=1)
        process.communicate.return_value = ("", "")
        popen.return_value = process
        run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="73400324\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="800\n", stderr=""),
            subprocess.TimeoutExpired([], 2),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]

        self.assertIsNone(_select_project_directory(None))

        self.assertEqual(run.call_args_list[3].args[0], [
            "/usr/bin/xdotool", "windowraise", "800",
        ])

    @patch("pilferedparrot.web.shutil.which", return_value=None)
    def test_missing_native_chooser_preserves_manual_entry_fallback(self, _which):
        with self.assertRaisesRegex(RuntimeError, "enter the project folder path manually"):
            _select_project_directory(None)

    @patch("pilferedparrot.web._select_project_directory", return_value=Path.home().resolve())
    def test_qwen_selection_keeps_workspace_safety_checks(self, _select):
        app = object.__new__(PilferedParrotApp)
        app.config = load_config()
        app.default_provider = "qwen"
        with self.assertRaisesRegex(ValueError, "allow_home_workspace"):
            app.choose_project_directory({}, provider="qwen")


class WebConfigurationTests(unittest.TestCase):
    def test_ipv6_loopback_url_is_bracketed(self):
        server = MagicMock()
        server.server_address = ("::1", 43127)
        server.serve_forever.side_effect = KeyboardInterrupt
        app = MagicMock()
        app.recover_interrupted.return_value = 0

        with tempfile.TemporaryDirectory() as directory, \
                patch("pilferedparrot.web.PilferedParrotApp", return_value=app), \
                patch("pilferedparrot.web._IPv6ThreadingHTTPServer", return_value=server) as factory:
            self.assertEqual(serve({"web": {"host": "::1", "port": 43127}}, Path(directory),
                                   open_browser=False), 0)

        self.assertEqual(_IPv6ThreadingHTTPServer.address_family, socket.AF_INET6)
        factory.assert_called_once()
        app.persist_dashboard_capability.assert_called_once_with("http://[::1]:43127")

    def test_ephemeral_port_is_used_in_generated_url(self):
        server = MagicMock()
        server.server_address = ("127.0.0.1", 43128)
        server.serve_forever.side_effect = KeyboardInterrupt
        app = MagicMock()
        app.dashboard_capability = "dashboard-token"
        app.recover_interrupted.return_value = 0
        timer = MagicMock()

        with tempfile.TemporaryDirectory() as directory, \
                patch("pilferedparrot.web.PilferedParrotApp", return_value=app), \
                patch("pilferedparrot.web.ThreadingHTTPServer", return_value=server), \
                patch("pilferedparrot.web._pilferedparrot_status") as status, \
                patch("pilferedparrot.web.threading.Timer", return_value=timer) as timer_factory, \
                patch("pilferedparrot.web.webbrowser.open") as browser_open:
            self.assertEqual(serve({"web": {"host": "127.0.0.1", "port": 0}}, Path(directory),
                                   open_browser=True), 0)
            timer_factory.call_args.args[1]()

        status.assert_not_called()
        app.persist_dashboard_capability.assert_called_once_with("http://127.0.0.1:43128")
        browser_open.assert_called_once_with(
            f"http://127.0.0.1:43128/?generation={API_GENERATION}&assets={ASSET_VERSION}"
            f"&runtime={RUNTIME_VERSION}#capability=dashboard-token",
        )

    def test_ephemeral_port_is_used_for_host_and_origin_validation(self):
        app = MagicMock()
        app.config = {"web": {"host": "127.0.0.1", "port": 0}}
        app.capability_context.return_value = {"scope": "dashboard"}
        handler_type = make_handler(app)
        handler = object.__new__(handler_type)
        handler.server = MagicMock(server_address=("127.0.0.1", 43129))
        handler.client_address = ("127.0.0.1", 50000)
        handler.headers = {
            "Host": "127.0.0.1:43129",
            "Origin": "http://127.0.0.1:43129",
            "X-PilferedParrot-Capability": "dashboard-token",
        }

        self.assertTrue(handler._control_allowed())


if __name__ == "__main__":
    unittest.main()
