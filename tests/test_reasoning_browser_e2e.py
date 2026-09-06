"""Browser coverage for work and Chat reasoning-effort controls."""

from __future__ import annotations

import json
import os
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

try:
    from playwright.sync_api import expect, sync_playwright
except ModuleNotFoundError:
    if os.environ.get("PILFEREDPARROT_REQUIRE_PLAYWRIGHT") == "1":
        raise
    expect = sync_playwright = None

import playwright_fixture as fixture_module
from playwright_fixture import BrowserTestApp, FakeProvider, PilferedParrotBrowserFixture


class ReasoningProvider(FakeProvider):
    """Fake provider that retains the config passed to each dispatch."""

    def __init__(self) -> None:
        super().__init__()
        self.configs: list[dict] = []

    def dispatch(self, provider, prompt, cwd, conversation, config, cancel_event):
        self.configs.append(config)
        return super().dispatch(provider, prompt, cwd, conversation, config, cancel_event)


class ReasoningApp(BrowserTestApp):
    def poll_provider_models(self, provider: str):
        if provider != "codex":
            raise ValueError("unknown fake provider")
        return {
            "provider": "codex", "default": "fake-small", "source": "browser_fixture",
            "options": [
                {"value": "fake-small", "label": "Fake Small",
                 "reasoning_efforts": ["low", "medium", "high"],
                 "default_reasoning_effort": "low"},
                {"value": "fake-large", "label": "Fake Large",
                 "reasoning_efforts": ["low", "medium", "high", "xhigh"],
                 "default_reasoning_effort": "medium"},
            ],
        }


class ReasoningFixture(PilferedParrotBrowserFixture):
    """Fixture with model-specific reasoning capabilities."""

    def __init__(self):
        with patch.object(fixture_module, "BrowserTestApp", ReasoningApp):
            super().__init__()
        # The parent installs its provider's bound method while constructing the
        # app. Replace it after construction so every request is captured here.
        self.provider = ReasoningProvider()
        fixture_module.web.capture_dispatch = self.provider.dispatch

    def _config(self):
        config = super()._config()
        cache_path = config["codex"]["models_cache"]
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump({"models": [
                {"slug": "fake-small", "display_name": "Fake Small",
                 "supported_reasoning_levels": [
                     {"effort": "low", "description": "Low"},
                     {"effort": "medium", "description": "Medium"},
                     {"effort": "high", "description": "High"},
                 ], "default_reasoning_level": "low"},
                {"slug": "fake-large", "display_name": "Fake Large",
                 "supported_reasoning_levels": [
                     {"effort": "low", "description": "Low"},
                     {"effort": "medium", "description": "Medium"},
                     {"effort": "high", "description": "High"},
                     {"effort": "xhigh", "description": "Extra high"},
                 ], "default_reasoning_level": "medium"},
            ]}, handle)
        return config


@unittest.skipUnless(sync_playwright, "install requirements-browser.txt to run Playwright")
class ReasoningBrowserEndToEndTests(unittest.TestCase):
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
        self.fixture = ReasoningFixture()
        self.addCleanup(self.fixture.stop)
        self.fixture.app.reset_chat({"model": "fake-small", "provider": "codex"})
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.external_requests = []
        self.page_errors = []
        self.context.on("request", self._record_external_request)
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.page_errors.append(error))
        self.page.goto(self.fixture.browser_url, wait_until="domcontentloaded")
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled(timeout=5_000)

    def tearDown(self):
        self.assertEqual(self.external_requests, [])
        self.assertEqual(self.page_errors, [])

    def _record_external_request(self, request):
        if urlparse(request.url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            self.external_requests.append(request.url)

    def _work_effort(self):
        return self.page.locator("#reasoningSelect")

    def _send_work(self, prompt):
        self.page.get_by_role("textbox", name="Message").fill(prompt)
        self.page.get_by_role("button", name="Send").click()

    def test_work_reasoning_survives_reload_and_is_sent_to_dispatch(self):
        model = self.page.get_by_role("combobox", name="Model")
        with self.page.expect_response("**/api/preferences/provider"):
            model.select_option("fake-large")
        effort = self._work_effort()
        expect(effort).to_be_enabled()
        expect(effort).to_have_value("")
        with self.page.expect_response("**/api/chats/*/reasoning") as response:
            effort.select_option("xhigh")
        self.assertTrue(response.value.ok)
        self.page.reload(wait_until="domcontentloaded")
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled()
        expect(self._work_effort()).to_have_value("xhigh")

        prompt = "work reasoning dispatch"
        self._send_work(prompt)
        expect(self.page.get_by_text(f"Fake provider completed: {prompt}", exact=True)).to_be_visible(timeout=5_000)
        self.assertEqual(self.fixture.provider.configs[-1]["codex"]["reasoning_effort"], "xhigh")

    def test_switching_to_model_without_choice_resets_work_effort(self):
        model = self.page.get_by_role("combobox", name="Model")
        with self.page.expect_response("**/api/preferences/provider"):
            model.select_option("fake-large")
        expect(self._work_effort()).to_be_enabled()
        self._work_effort().select_option("xhigh")
        with self.page.expect_response("**/api/preferences/provider"):
            model.select_option("fake-small")
        expect(self._work_effort()).to_be_enabled()
        expect(self._work_effort()).to_have_value("")
        self.assertEqual(self._work_effort().locator('option[value="xhigh"]').count(), 0)

    def test_work_reasoning_is_disabled_during_run_but_work_thread_is_resumable(self):
        prompt = "held reasoning run"
        self.fixture.provider.hold(prompt)
        self._send_work(prompt)
        expect(self._work_effort()).to_be_disabled()
        self.fixture.provider.complete(prompt)
        expect(self.page.get_by_text(f"Fake provider completed: {prompt}", exact=True)).to_be_visible(timeout=5_000)
        expect(self._work_effort()).to_be_enabled()

    def test_chat_reasoning_survives_reload_and_is_sent_with_chat_message(self):
        chat_capability = self.fixture.app.issue_capability("chat", provider="codex")
        chat = self.context.new_page()
        chat.on("pageerror", lambda error: self.page_errors.append(error))
        chat.goto(
            f"{self.fixture.base_url}/chat#capability={chat_capability}&provider=codex",
            wait_until="domcontentloaded",
        )
        chat.wait_for_load_state("domcontentloaded")
        expect(chat.get_by_role("textbox", name="Message Chat")).to_be_enabled(timeout=5_000)
        effort = chat.locator("#chatReasoningSelect")
        expect(effort).to_have_attribute("aria-label", "Chat reasoning effort")
        with chat.expect_response("**/api/chat/reasoning") as response:
            effort.select_option("high")
        self.assertTrue(response.value.ok)
        chat.reload(wait_until="domcontentloaded")
        expect(effort).to_have_value("high")
        prompt = "chat reasoning dispatch"
        chat.get_by_role("textbox", name="Message Chat").fill(prompt)
        with chat.expect_request("**/api/chat/messages") as request:
            chat.get_by_role("button", name="Send to Chat").click()
        self.assertEqual(request.value.post_data_json["reasoning_effort"], "high")
        expect(chat.locator("article.chat-message.assistant").last).to_contain_text(prompt, timeout=5_000)
        self.assertEqual(self.fixture.provider.configs[-1]["codex"]["reasoning_effort"], "high")
        with chat.expect_response("**/api/chat/reset") as response:
            chat.get_by_role("button", name="Start a new chat").click()
        self.assertTrue(response.value.ok)
        old = chat.get_by_role("button", name=re.compile(r"^chat reasoning dispatch"))
        expect(old).to_contain_text("Archived")
        old.click()
        expect(chat.locator("#chatReasoningSelect")).to_be_disabled()

    def test_reasoning_controls_and_sidebar_details_are_responsive(self):
        for width in (390, 1280):
            self.page.set_viewport_size({"width": width, "height": 844})
            expect(self.page.locator("#composer")).to_be_visible()
            self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), width)
        for details_id in ("contextDetails", "preferencesDetails"):
            details = self.page.locator(f"#{details_id}")
            expect(details).not_to_have_attribute("open", "")
            summary = details.locator(":scope > summary")
            expect(summary).to_be_visible()
            if details_id == "preferencesDetails":
                expect(self.page.locator("#notificationPreferences")).to_be_hidden()
                expect(self.page.locator("#chromeTheme")).to_be_hidden()
            summary.click()
            expect(details).to_have_attribute("open", "")
        expect(self.page.locator("#notificationPreferences")).to_be_visible()
        expect(self.page.locator("#chromeTheme")).to_be_visible()
