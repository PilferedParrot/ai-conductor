"""Browser regressions for provider onboarding and its dashboard layout.

The production server and assets run here. Only the provider, model-poll, and
budget boundaries are fake, so these tests never require a CLI, an account, or
an outbound connection.
"""

from __future__ import annotations

import os
import threading
import unittest
from urllib.parse import urlparse
from unittest.mock import patch

try:
    from playwright.sync_api import expect, sync_playwright
except ModuleNotFoundError:
    if os.environ.get("PILFEREDPARROT_REQUIRE_PLAYWRIGHT") == "1":
        raise
    expect = sync_playwright = None

import playwright_fixture as fixture_module
from playwright_fixture import BrowserTestApp, FakeBudget, PilferedParrotBrowserFixture


class OnboardingApp(BrowserTestApp):
    """Production app with deterministic native-provider boundaries."""

    def budgets(self):
        gate = getattr(self, "budget_gate", None)
        if gate is not None and not gate.wait(timeout=10):
            raise TimeoutError("browser test did not release the budget refresh")
        error = getattr(self, "fixture_budget_error", None)
        if error is not None:
            raise error
        status = getattr(self, "budget_state", "cli_missing")
        return {
            provider: FakeBudget(
                provider,
                available=False,
                status=status,
                auth_status="signed_out",
                reachability="unreachable",
            )
            for provider in ("codex", "claude", "gemini", "antigravity")
        }

    def poll_provider_models(self, provider: str):
        error = getattr(self, "model_poll_error", None)
        if error is not None:
            raise error
        payload = getattr(self, "model_poll_payload", None)
        if payload is not None:
            return {"provider": provider, **payload}
        return {
            "provider": provider,
            "default": "saved-model",
            "source": "browser_fixture",
            "options": [
                {"value": "saved-model", "label": "Saved model"},
                {"value": "saved-large", "label": "Saved large"},
            ],
        }


class OnboardingFixture(PilferedParrotBrowserFixture):
    """Own config, state, and loopback server for each onboarding test."""

    def _config(self):
        config = super()._config()
        # Exercise every native card without discovering a host CLI or touching
        # host config/state. Qwen is outside this native onboarding flow.
        config["_hidden_providers"] = ["qwen"]
        for provider in ("codex", "claude", "gemini", "antigravity"):
            config[provider]["command"] = str(self.root / f"never-invoked-{provider}")
            config[provider]["config_path"] = str(self.root / f"{provider}-config")
            config[provider]["models_cache"] = str(self.root / f"{provider}-models.json")
            config[provider]["model"] = "saved-model"
            config[provider]["model_options"] = ["saved-model", "saved-large"]
        return config

    def __init__(self):
        # Do not use include_claude: its fixture capability intentionally makes
        # Claude the initial window provider. This suite starts as Codex.
        with patch.object(fixture_module, "BrowserTestApp", OnboardingApp):
            super().__init__(include_claude=False)
        self.app.budget_gate = None
        self.app.fixture_budget_error = None
        self.app.budget_state = "cli_missing"
        self.app.model_poll_error = None
        self.app.model_poll_payload = None


@unittest.skipUnless(sync_playwright, "install requirements-browser.txt to run Playwright")
class OnboardingBrowserEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except BaseException:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.fixture = OnboardingFixture()
        self.addCleanup(self.fixture.stop)
        self.context = self.browser.new_context(viewport={"width": 1920, "height": 1008})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.external_requests = []
        self.page_errors = []
        self.page.route("**/*", self._allow_fixture_origin_only)
        self.page.on("pageerror", lambda error: self.page_errors.append(error))
        self.page.goto(self.fixture.browser_url, wait_until="domcontentloaded")
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled(timeout=5_000)

    def tearDown(self):
        self.assertEqual(self.external_requests, [], "browser requested an origin outside its fixture")
        self.assertEqual(self.page_errors, [], "browser emitted an unhandled JavaScript error")

    def _allow_fixture_origin_only(self, route):
        request = route.request
        parsed = urlparse(request.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin == self.fixture.base_url:
            route.continue_()
            return
        self.external_requests.append(request.url)
        route.abort()

    def _open_dashboard(self):
        opener = self.page.get_by_role("button", name="Provider dashboard")
        expect(opener).to_be_enabled()
        opener.click()
        dialog = self.page.locator("#providerDialog")
        expect(dialog).to_be_visible()
        expect(dialog.locator(".provider-connection-head > small").first).to_contain_text("CLI not found")
        return dialog

    def _card(self, dialog, provider: str):
        return dialog.locator(".provider-connection-card").filter(
            has=self.page.locator(f'[data-provider-window="{provider}"]'),
        )

    def _gate_budget_refresh(self):
        gate = threading.Event()
        # This cleanup is registered before the network request. A failed UI
        # expectation therefore cannot leave the fixture server waiting on it.
        self.addCleanup(gate.set)
        self.fixture.app.budget_gate = gate
        self.fixture.app._invalidate_budgets()
        return gate

    def _refresh_dashboard_while_gated(self, dialog, gate):
        refresh = dialog.locator("#refreshProviderDashboard")
        with self.page.expect_response("**/api/budgets") as response_info:
            refresh.click()
            expect(refresh).to_contain_text("Checking…")
            expect(refresh).to_have_attribute("aria-busy", "true")
            expect(dialog.locator("#providerDashboardStatus")).to_have_text(
                "Checking provider status…",
            )
            gate.set()
        return response_info.value

    def test_desktop_and_narrow_sidebar_keep_the_visible_action_labels(self):
        sidebar = self.page.get_by_role("navigation", name="Workspace actions")
        expect(sidebar.get_by_text("Providers", exact=True)).to_be_visible()
        expect(sidebar.get_by_text("New Session", exact=True)).to_be_visible()
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 1920)

        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.get_by_role("button", name="Open sidebar").click()
        expect(sidebar).to_be_visible()
        expect(sidebar.get_by_text("Providers", exact=True)).to_be_visible()
        expect(sidebar.get_by_text("New Session", exact=True)).to_be_visible()
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)

    def test_project_dialog_shows_folder_basename_supports_browse_and_safe_manual_text(self):
        project_button = self.page.locator("#projectButton")
        expect(project_button).to_be_enabled()
        project_button.click()

        dialog = self.page.locator("#projectDialog")
        project_input = dialog.locator("#projectInput")
        folder_name = dialog.locator("#projectFolderName")
        expect(dialog).to_be_visible()
        expect(project_input).to_have_value(str(self.fixture.project))
        expect(folder_name).to_have_text("project")
        desktop_box = dialog.bounding_box()
        self.assertIsNotNone(desktop_box)
        self.assertGreater(desktop_box["width"], 470)
        self.assertLessEqual(desktop_box["width"], 722)

        selected = str(self.fixture.root / "picked-project") + "/"
        with patch.object(
            self.fixture.app,
            "choose_project_directory",
            side_effect=[{"path": selected}, {"path": None}],
        ):
            with self.page.expect_response("**/api/project/folder") as browse_response:
                dialog.get_by_role("button", name="Browse…").click()
            self.assertTrue(browse_response.value.ok)
            expect(project_input).to_have_value(selected)
            expect(folder_name).to_have_text("picked-project")
            expect(project_input).to_be_focused()

            # A cancelled native chooser leaves the current manual selection intact.
            with self.page.expect_response("**/api/project/folder") as cancel_response:
                dialog.get_by_role("button", name="Browse…").click()
            self.assertTrue(cancel_response.value.ok)
            expect(project_input).to_have_value(selected)
            expect(folder_name).to_have_text("picked-project")

        # Windows separators must also render a basename on every test host.
        project_input.fill("C:\\Projects\\parrot\\")
        expect(folder_name).to_have_text("parrot")

        unsafe_path = "/tmp/<img src=x onerror=alert(1)>/"
        project_input.fill(unsafe_path)
        expect(folder_name).to_have_text("<img src=x onerror=alert(1)>")
        expect(folder_name).to_have_attribute("title", unsafe_path)
        self.assertEqual(dialog.locator("script, img, iframe, object").count(), 0)

        long_name = "long-project-" * 20
        project_input.fill("/tmp/" + long_name)
        expect(folder_name).to_have_text(long_name)
        self.page.set_viewport_size({"width": 390, "height": 844})
        expect(dialog).to_be_visible()
        dialog_box = dialog.bounding_box()
        self.assertIsNotNone(dialog_box)
        self.assertLessEqual(dialog_box["width"], 390)
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)
        self.assertTrue(dialog.evaluate("node => node.scrollWidth <= node.clientWidth"))

    def test_dashboard_heading_dot_and_status_have_readable_non_overflowing_layout(self):
        dialog = self._open_dashboard()
        for provider, label in (
            ("codex", "OpenAI Codex"),
            ("claude", "Claude Code"),
            ("gemini", "Google Gemini"),
        ):
            card = self._card(dialog, provider)
            heading = card.get_by_text(label, exact=True)
            dot = card.locator(".provider-connection-head .status-dot")
            status = card.locator(".provider-connection-head > small")
            expect(heading).to_be_visible()
            expect(dot).to_be_visible()
            expect(status).to_be_visible()
            heading_box, dot_box, status_box = heading.bounding_box(), dot.bounding_box(), status.bounding_box()
            self.assertIsNotNone(heading_box)
            self.assertIsNotNone(dot_box)
            self.assertIsNotNone(status_box)
            self.assertLess(dot_box["x"], heading_box["x"])
            self.assertLessEqual(
                abs(
                    (dot_box["y"] + dot_box["height"] / 2)
                    - (heading_box["y"] + heading_box["height"] / 2),
                ),
                heading_box["height"] / 2,
            )
            self.assertGreaterEqual(status_box["y"], heading_box["y"] + heading_box["height"])
            self.assertGreaterEqual(status.evaluate("node => parseFloat(getComputedStyle(node).fontSize)"), 16)
            self.assertFalse(card.evaluate("node => node.scrollWidth > node.clientWidth"))
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 1920)

    def test_cli_cards_install_first_then_offer_signin_while_window_access_stays_enabled(self):
        dialog = self._open_dashboard()
        for provider in ("codex", "claude"):
            card = self._card(dialog, provider)
            expect(card.get_by_text("Install CLI first", exact=True)).to_be_disabled()
            expect(card.locator(f'[data-provider-login="{provider}"]')).to_have_count(0)
            expect(card.locator(f'[data-provider-window="{provider}"]')).to_be_enabled()

        self.fixture.app.budget_state = "signed_out"
        response = self._refresh_dashboard_while_gated(dialog, self._gate_budget_refresh())
        self.assertTrue(response.ok)
        expect(dialog.locator("#providerDashboardStatus")).to_have_text("Status refreshed.")
        for provider in ("codex", "claude"):
            card = self._card(dialog, provider)
            expect(card.locator(f'[data-provider-login="{provider}"]')).to_be_enabled()
            expect(card.locator(f'[data-provider-window="{provider}"]')).to_be_enabled()

    def test_google_gemini_is_install_and_terminal_guidance_without_an_in_app_signin(self):
        dialog = self._open_dashboard()
        card = self._card(dialog, "gemini")
        expect(card).to_contain_text("Install the Gemini CLI")
        expect(card).to_contain_text("run gemini in a terminal")
        expect(card.get_by_text("Uses your Gemini CLI authentication", exact=True)).to_be_visible()
        expect(card).to_contain_text("API key or supported organization account")
        expect(card.locator('[data-provider-login="gemini"]')).to_have_count(0)
        expect(card.get_by_text("Install CLI first", exact=True)).to_have_count(0)
        expect(card.locator('[data-provider-window="gemini"]')).to_be_enabled()

    def test_antigravity_card_explains_terminal_setup_and_work_only_capability(self):
        dialog = self._open_dashboard()
        card = self._card(dialog, "antigravity")
        expect(card).to_contain_text("Install Antigravity CLI")
        expect(card).to_contain_text("Work only")
        expect(card).to_contain_text("run agy in a terminal")
        expect(card.locator('[data-provider-login="antigravity"]')).to_have_count(0)
        expect(card.locator('[data-provider-window="antigravity"]')).to_be_enabled()

    def test_dashboard_refresh_reports_pending_completion_and_error_inside_dialog(self):
        dialog = self._open_dashboard()
        response = self._refresh_dashboard_while_gated(dialog, self._gate_budget_refresh())
        self.assertTrue(response.ok)
        expect(dialog.locator("#providerDashboardStatus")).to_have_text("Status refreshed.")
        expect(dialog.locator("#refreshProviderDashboard")).to_contain_text("Refresh status")

        self.fixture.app.fixture_budget_error = RuntimeError("synthetic provider failure")
        error_response = self._refresh_dashboard_while_gated(dialog, self._gate_budget_refresh())
        self.assertEqual(error_response.status, 500)
        expect(dialog.locator("#providerDashboardStatus")).to_have_text(
            "Could not refresh status. Try again.",
        )

    def test_model_warning_and_http_error_are_plain_language_in_dialog_and_keep_focus(self):
        dialog = self._open_dashboard()
        select = dialog.locator('[data-provider-model="codex"]')
        select.select_option("saved-large")
        self.fixture.app.model_poll_payload = {
            "default": "saved-model",
            "options": [
                {"value": "saved-model", "label": "Saved model"},
                {"value": "saved-large", "label": "Saved large"},
            ],
            "warning": "fixture model source is unavailable",
        }
        select.evaluate("node => node.focus()")
        # Dispatching the delegated pointer event requests a refresh without
        # opening the browser's native select menu in headless runs.
        with self.page.expect_response("**/api/providers/codex/models") as warning_response:
            select.dispatch_event("pointerdown")
        self.assertTrue(warning_response.value.ok)
        warning = dialog.locator('[data-provider-model-status="codex"]')
        expect(warning).to_have_text("Could not refresh models. Saved models are still available.")
        expect(select).to_have_value("saved-large")
        expect(select).to_be_focused()

        self.fixture.app.model_poll_payload = None
        self.fixture.app.model_poll_error = RuntimeError("synthetic model failure")
        select.evaluate("node => node.focus()")
        with self.page.expect_response("**/api/providers/codex/models") as error_response:
            select.dispatch_event("pointerdown")
        self.assertEqual(error_response.value.status, 500)
        expect(warning).to_have_text("Could not refresh models. Saved models are still available.")
        expect(select).to_have_value("saved-large")
        expect(select).to_be_focused()

    def test_escape_closes_dashboard_and_returns_focus_to_its_opener(self):
        opener = self.page.get_by_role("button", name="Provider dashboard")
        dialog = self._open_dashboard()
        self.page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(opener).to_be_focused()


if __name__ == "__main__":
    unittest.main()
