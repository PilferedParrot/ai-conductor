"""Browser coverage for desktop-notification preference and toast UX."""

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
class BrowserNotificationTests(unittest.TestCase):
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
        self.page = self.context.new_page()
        self.page.add_init_script("""
          class MockNotification {
            static permission = "default";
            static calls = 0;
            static async requestPermission() {
              MockNotification.calls += 1;
              MockNotification.permission = "denied";
              return "denied";
            }
            constructor() {}
          }
          Object.defineProperty(globalThis, "Notification", {
            configurable: true,
            writable: true,
            value: MockNotification,
          });
        """)
        self.page.goto(self.fixture.browser_url, wait_until="domcontentloaded")
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled(timeout=5_000)

    def test_permission_request_is_persisted_until_explicit_reset(self):
        control = self.page.get_by_role("button", name="Enable desktop notifications")
        control.click()
        expect(self.page.get_by_role("button", name="Desktop notifications denied · Reset")).to_be_visible()
        self.assertEqual(self.page.evaluate("Notification.calls"), 1)

        self.page.reload(wait_until="domcontentloaded")
        expect(self.page.get_by_role("button", name="Desktop notifications denied · Reset")).to_be_visible()
        self.assertEqual(self.page.evaluate("Notification.calls"), 1)

        self.page.get_by_role("button", name="Desktop notifications denied · Reset").click()
        expect(self.page.get_by_role("button", name="Enable desktop notifications")).to_be_visible()
        self.page.get_by_role("button", name="Enable desktop notifications").click()
        self.assertEqual(self.page.evaluate("Notification.calls"), 2)

    def test_toast_is_opaque_readable_and_persists_long_enough_to_read(self):
        self.page.evaluate('toast("A successful action", "success")')
        toast = self.page.locator("#toast")
        expect(toast).to_be_visible()
        style = toast.evaluate("node => { const s = getComputedStyle(node); return { background: s.backgroundColor, border: s.borderColor, color: s.color, fontSize: parseFloat(s.fontSize) }; }")
        self.assertNotEqual(style["background"], "rgba(0, 0, 0, 0)")
        self.assertNotEqual(style["background"], style["color"])
        self.assertNotEqual(style["border"], style["background"])
        self.assertGreaterEqual(style["fontSize"], 16)
        self.page.wait_for_timeout(3_000)
        expect(toast).to_be_visible()
        self.page.evaluate('toast("A warning", "warning")')
        self.assertNotEqual(
            toast.evaluate("node => getComputedStyle(node).backgroundColor"), style["background"],
        )
        self.page.evaluate('toast("An error", "error")')
        self.assertNotEqual(
            toast.evaluate("node => getComputedStyle(node).backgroundColor"), style["background"],
        )


if __name__ == "__main__":
    unittest.main()
