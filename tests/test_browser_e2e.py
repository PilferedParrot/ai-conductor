"""Headless end-to-end tests for the real PilferedParrot browser UI."""

from __future__ import annotations

import os
import re
import unittest
from urllib.parse import urlparse

try:
    from playwright.sync_api import expect, sync_playwright
except ModuleNotFoundError:
    if os.environ.get("PILFEREDPARROT_REQUIRE_PLAYWRIGHT") == "1":
        raise
    expect = sync_playwright = None

from playwright_fixture import FakeProvider, PilferedParrotBrowserFixture


@unittest.skipUnless(sync_playwright, "install requirements-browser.txt to run Playwright")
class BrowserEndToEndTests(unittest.TestCase):
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
        self.fixture = PilferedParrotBrowserFixture()
        self.addCleanup(self.fixture.stop)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.external_requests = []
        self.context.on("request", self._record_external_request)
        self.page = self.context.new_page()
        self.page_errors = []
        self.page.on("pageerror", self.page_errors.append)
        self.page.goto(self.fixture.browser_url, wait_until="domcontentloaded")
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled(timeout=5_000)

    def tearDown(self):
        self.assertEqual(self.external_requests, [], "browser fixture attempted external network")
        self.assertEqual(self.page_errors, [], "browser emitted an unhandled JavaScript error")

    def _record_external_request(self, request):
        host = urlparse(request.url).hostname
        if host not in {"127.0.0.1", "localhost", "::1"}:
            self.external_requests.append(request.url)

    def _send(self, prompt: str) -> None:
        message = self.page.get_by_role("textbox", name="Message")
        message.fill(prompt)
        expect(self.page.get_by_role("button", name="Send")).to_be_enabled()
        self.page.get_by_role("button", name="Send").click()

    def test_initial_application_load_renders_core_ui(self):
        expect(self.page).to_have_title("PilferedParrot")
        expect(self.page.get_by_role("heading", name="What are we working on?")).to_be_visible()
        workspace_actions = self.page.get_by_role("navigation", name="Workspace actions")
        if workspace_actions.count():
            expect(workspace_actions).to_be_visible()
            expect(self.page.get_by_role("button", name="Provider dashboard")).to_be_enabled()
        expect(self.page.get_by_role("button", name="Start a new work session")).to_be_enabled()
        expect(self.page.get_by_role("combobox", name="Model")).to_have_value("fake-small")
        expect(self.page.get_by_text("OpenAI Codex", exact=True).first).to_be_visible()
        signed_in = self.page.get_by_text("Signed in", exact=True)
        if signed_in.count():
            expect(signed_in.first).to_be_visible()
        expect(self.page.locator("#projectButton")).to_have_text(str(self.fixture.project))

    def test_model_preference_and_capability_survive_reload(self):
        expect(self.page).to_have_url(f"{self.fixture.base_url}/")
        model = self.page.get_by_role("combobox", name="Model")
        modern_ui = self.page.get_by_role("button", name="Provider dashboard").count() > 0
        if modern_ui:
            with self.page.expect_response("**/api/preferences/provider") as response:
                model.select_option("fake-large")
            self.assertTrue(response.value.ok)
        else:
            model.select_option("fake-large")

        self.page.reload(wait_until="domcontentloaded")

        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled()
        expect(self.page.get_by_role("combobox", name="Model")).to_have_value(
            "fake-large" if modern_ui else "fake-small",
        )
        expect(self.page).to_have_url(f"{self.fixture.base_url}/")

    def test_request_shows_progress_then_completed_fake_result(self):
        prompt = "Complete this deterministic browser request"
        self.fixture.provider.hold(prompt)

        self._send(prompt)

        expect(self.page.get_by_label("OpenAI Codex is working")).to_be_visible()
        expect(self.page.get_by_text("Preparing deterministic response", exact=True)).to_be_visible(timeout=5_000)
        self.fixture.provider.complete(prompt)
        expect(self.page.get_by_text(f"Fake provider completed: {prompt}", exact=True)).to_be_visible(timeout=5_000)
        expect(self.page.get_by_label("OpenAI Codex is working")).to_have_count(0)
        self.assertEqual(self.fixture.provider.requests[0][1:], (prompt, self.fixture.project))

    def test_provider_error_is_rendered_and_composer_recovers(self):
        self._send(FakeProvider.ERROR_PROMPT)

        error = self.page.locator("article.message.error")
        expect(error.get_by_text(FakeProvider.ERROR_TEXT, exact=True)).to_be_visible(timeout=5_000)
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled()
        expect(self.page.get_by_role("button", name="Cancel response")).to_be_hidden()

    def test_session_navigation_and_active_session_restore_after_reload(self):
        prompt = "Returnable session"
        self._send(prompt)
        expect(self.page.get_by_text(f"Fake provider completed: {prompt}", exact=True)).to_be_visible(timeout=5_000)

        with self.page.expect_response("**/api/chats") as response:
            self.page.get_by_role("button", name="Start a new work session").click()
        self.assertTrue(response.value.ok)
        sessions = self.page.get_by_role("navigation", name="Session history").get_by_role("button")
        expect(sessions).to_have_count(2)
        old_session = self.page.get_by_role("button", name=re.compile(r"^Returnable session"))
        old_session.click()
        expect(self.page.locator("#chatTitle")).to_have_text(prompt)

        self.page.reload(wait_until="domcontentloaded")

        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled()
        if self.page.get_by_role("button", name="Provider dashboard").count():
            expect(self.page.locator("#chatTitle")).to_have_text(prompt)
            expect(self.page.get_by_text(f"Fake provider completed: {prompt}", exact=True)).to_be_visible()
        else:
            expect(sessions).to_have_count(2)


if __name__ == "__main__":
    unittest.main()
