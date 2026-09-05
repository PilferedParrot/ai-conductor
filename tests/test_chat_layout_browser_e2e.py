"""Browser coverage for Chat's responsive layout."""

from __future__ import annotations

import os
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ModuleNotFoundError:
    if os.environ.get("PILFEREDPARROT_REQUIRE_PLAYWRIGHT") == "1":
        raise
    expect = sync_playwright = None

from playwright_fixture import PilferedParrotBrowserFixture


@unittest.skipUnless(sync_playwright, "install requirements-browser.txt to run Playwright")
class ChatLayoutBrowserEndToEndTests(unittest.TestCase):
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
        self.context = self.browser.new_context(
            viewport={"width": 320, "height": 844}, reduced_motion="reduce",
        )
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(error))
        capability = self.fixture.app.issue_capability("chat", provider="codex")
        self.page.goto(
            f"{self.fixture.base_url}/chat#capability={capability}&provider=codex",
            wait_until="domcontentloaded",
        )
        expect(self.page.get_by_role("textbox", name="Message Chat")).to_be_enabled(timeout=5_000)

    def tearDown(self):
        self.assertEqual(self.page_errors, [], "browser emitted an unhandled JavaScript error")

    def _assert_layout(self, *, send_enabled):
        for width in (320, 390, 600, 1024):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 844})
                self.page.wait_for_function("width => window.innerWidth === width", arg=width)
                metrics = self.page.evaluate(
                    """() => {
                        const rect = selector => document.querySelector(selector).getBoundingClientRect();
                        const header = rect('.chat-header');
                        const title = rect('#chatThreadTitle');
                        const composer = rect('.chat-composer');
                        const controls = rect('.chat-composer-bar .composer-controls');
                        const model = rect('#chatModelSelect');
                        const reasoning = rect('#chatReasoningSelect');
                        const send = rect('#sendChat');
                        return {
                            viewport: document.documentElement.clientWidth,
                            documentScrollWidth: document.documentElement.scrollWidth,
                            bodyScrollWidth: document.body.scrollWidth,
                            headerRight: header.right,
                            titleLeft: title.left,
                            titleRight: title.right,
                            composerRight: composer.right,
                            controlsRight: controls.right,
                            controlsLeft: controls.left,
                            modelRight: model.right,
                            modelLeft: model.left,
                            modelWidth: model.width,
                            reasoningRight: reasoning.right,
                            reasoningLeft: reasoning.left,
                            reasoningWidth: reasoning.width,
                            sendRight: send.right,
                            sendLeft: send.left,
                            sendWidth: send.width,
                            sendDisabled: document.querySelector('#sendChat').disabled,
                        };
                    }"""
                )
                self.assertLessEqual(metrics["documentScrollWidth"], metrics["viewport"])
                self.assertLessEqual(metrics["bodyScrollWidth"], metrics["viewport"])
                self.assertEqual(metrics["sendDisabled"], not send_enabled)
                self.assertGreaterEqual(metrics["titleLeft"], -0.5)
                self.assertLessEqual(metrics["headerRight"], metrics["viewport"] + 0.5)
                self.assertLessEqual(metrics["titleRight"], metrics["viewport"] + 0.5)
                for key in ("composerRight", "controlsRight", "modelRight", "reasoningRight", "sendRight"):
                    self.assertLessEqual(metrics[key], metrics["viewport"] + 0.5, key)
                for key in ("controlsLeft", "modelLeft", "reasoningLeft", "sendLeft"):
                    self.assertGreaterEqual(metrics[key], -0.5, key)
                for key in ("modelWidth", "reasoningWidth", "sendWidth"):
                    self.assertGreater(metrics[key], 0, key)

    def test_chat_has_no_horizontal_overflow_and_composer_fits(self):
        # Cover the empty default screen before adding content that can expose
        # intrinsic-size regressions.
        self._assert_layout(send_enabled=False)

        # Exercise the same intrinsic-size paths that break narrow layouts in
        # practice: a selected long model label and a long message draft.
        self.page.locator("#chatModelSelect option").first.evaluate(
            "option => { option.textContent = 'codex-' + 'model-name-'.repeat(24); }"
        )
        self.page.locator("#chatPrompt").fill("long-message-" + "x" * 1200)
        self._assert_layout(send_enabled=True)


if __name__ == "__main__":
    unittest.main()
