"""Browser regressions for adding OpenAI-compatible provider cards.

The provider discovery boundary is deterministic here, so setup tests never
contact a real service or expose a credential.
"""

from __future__ import annotations

import os
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
from playwright_fixture import BrowserTestApp, PilferedParrotBrowserFixture


class ProviderSetupApp(BrowserTestApp):
    """Fake model discovery while retaining the production provider API."""

    def _discover_provider_models(self, base_url, api_key_env):
        del base_url, api_key_env
        self.discovery_calls = getattr(self, "discovery_calls", 0) + 1
        error = getattr(self, "discovery_error", None)
        if error is not None:
            raise error
        return ["fixture-model"]

    def poll_provider_models(self, provider: str):
        error = getattr(self, "model_poll_error", None)
        if error is not None:
            raise error
        return super().poll_provider_models(provider)


class ProviderSetupFixture(PilferedParrotBrowserFixture):
    def __init__(self):
        with patch.object(fixture_module, "BrowserTestApp", ProviderSetupApp):
            super().__init__()
        self.app.discovery_error = RuntimeError("deterministic discovery failure")
        self.app.model_poll_error = None
        self.app.discovery_calls = 0


@unittest.skipUnless(sync_playwright, "install requirements-browser.txt to run Playwright")
class ProviderSetupBrowserEndToEndTests(unittest.TestCase):
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
        self.fixture = ProviderSetupFixture()
        self.addCleanup(self.fixture.stop)
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page_errors = []
        self.external_requests = []
        self.page.on("pageerror", lambda error: self.page_errors.append(error))
        self.page.route("**/*", self._allow_fixture_origin_only)
        self.page.goto(self.fixture.browser_url, wait_until="domcontentloaded")
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled(timeout=5_000)

    def tearDown(self):
        self.assertEqual(self.external_requests, [])
        self.assertEqual(self.page_errors, [])

    def _allow_fixture_origin_only(self, route):
        request = route.request
        parsed = urlparse(request.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin == self.fixture.base_url:
            route.continue_()
        else:
            self.external_requests.append(request.url)
            route.abort()

    def _draft(self):
        self.page.get_by_role("button", name="Provider dashboard").click()
        dialog = self.page.locator("#providerDialog")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Add provider").click()
        draft = dialog.locator("[data-provider-draft]")
        expect(draft).to_be_visible()
        return dialog, draft

    def test_manual_model_id_adds_when_discovery_is_unavailable(self):
        dialog, draft = self._draft()
        draft.locator("[data-provider-draft-template]").select_option("custom")
        draft.locator("[data-provider-draft-label]").fill("Fixture Gateway")
        draft.locator("[data-provider-draft-model]").fill("account-specific-model")
        draft.locator("[data-provider-draft-base-url]").fill("http://127.0.0.1:9/v1")
        draft.locator("[data-provider-draft-api-key-env]").fill("FIXTURE_PROVIDER_KEY")
        draft.get_by_role("button", name="Add provider").click()

        self.assertEqual(self.fixture.app.discovery_calls, 0)
        self.assertEqual(self.fixture.app.default_provider, "codex")
        card = dialog.locator(".provider-connection-card").filter(
            has_text="Fixture Gateway",
        )
        expect(card).to_be_visible()
        expect(card.locator('[data-provider-model] option[value="account-specific-model"]')).to_be_attached()

    def test_coding_provider_templates_fill_connection_defaults_without_requests(self):
        _dialog, draft = self._draft()
        for template, url, variable in (
            ("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
            ("mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
            ("lmstudio", "http://127.0.0.1:1234/v1", ""),
        ):
            draft.locator("[data-provider-draft-template]").select_option(template)
            expect(draft.locator("[data-provider-draft-base-url]")).to_have_value(url)
            expect(draft.locator("[data-provider-draft-api-key-env]")).to_have_value(variable)
            expect(draft.locator("[data-provider-draft-model]")).to_have_value("")
        self.assertEqual(self.fixture.app.discovery_calls, 0)

    def test_failed_discovery_keeps_the_editable_draft(self):
        dialog, draft = self._draft()
        draft.locator("[data-provider-draft-template]").select_option("custom")
        draft.locator("[data-provider-draft-label]").fill("Unreachable Gateway")
        draft.locator("[data-provider-draft-base-url]").fill("http://127.0.0.1:9/v1")
        draft.locator("[data-provider-draft-api-key-env]").fill("FIXTURE_PROVIDER_KEY")
        draft.get_by_role("button", name="Add provider").click()

        expect(self.page.locator("#toast")).to_contain_text("deterministic discovery failure")
        layering = self.page.locator("#toast").evaluate("""
            node => {
                node.style.pointerEvents = 'auto';
                const rect = node.getBoundingClientRect();
                const hit = document.elementFromPoint(
                    rect.left + rect.width / 2, rect.top + rect.height / 2,
                );
                const result = {dialog: node.parentElement?.closest('dialog')?.id,
                    open: document.querySelector('#providerDialog')?.open,
                    hit: hit === node || hit?.closest('#toast') === node,
                    rect: [rect.left, rect.top, rect.width, rect.height],
                };
                node.style.pointerEvents = '';
                return result;
            }
        """)
        self.assertEqual(layering["dialog"], "providerDialog", layering)
        self.assertTrue(layering["open"], layering)
        self.assertTrue(layering["hit"], layering)
        self.assertEqual(self.fixture.app.discovery_calls, 1)
        self.assertEqual(self.fixture.provider.requests, [])
        self.assertEqual(self.fixture.app.default_provider, "codex")
        self.assertEqual(
            dialog.locator(".provider-connection-card").filter(has_text="Unreachable Gateway").count(),
            0,
        )
        expect(dialog.locator("[data-provider-draft]")).to_be_visible()
        expect(dialog.locator("[data-provider-draft-label]")).to_have_value("Unreachable Gateway")
        expect(dialog.locator("[data-provider-draft-model]")).to_have_value("")
        expect(dialog.locator("[data-provider-draft-base-url]")).to_have_value("http://127.0.0.1:9/v1")
        expect(dialog.locator("[data-provider-draft-api-key-env]")).to_have_value("FIXTURE_PROVIDER_KEY")

    def test_model_poll_failure_preserves_the_selected_model(self):
        dialog = self.page.get_by_role("button", name="Provider dashboard")
        dialog.click()
        panel = self.page.locator("#providerDialog")
        select = panel.locator('[data-provider-model="codex"]')
        select.select_option("fake-large")
        expect(select).to_have_value("fake-large")

        self.fixture.app.model_poll_error = RuntimeError("deterministic model poll failure")
        select.click()
        expect(panel.locator('[data-provider-model-status="codex"]')).to_have_text(
            "Could not refresh models. Saved models are still available.",
        )
        expect(select).to_have_value("fake-large")


if __name__ == "__main__":
    unittest.main()
