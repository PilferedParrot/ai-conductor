"""Browser coverage for the bounded Harness workflow."""

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
class HarnessBrowserEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

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
        self.page.goto(self.fixture.browser_url, wait_until="domcontentloaded")
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled(timeout=5_000)

    def plan(self):
        self.page.get_by_role("button", name="Harness").click()
        self.page.locator("#harnessPreset").select_option("sol-luna")
        self.page.locator("#harnessTask").fill("Implement <the bounded> fixture change")
        self.page.locator("#harnessCategory").select_option("implementation")
        self.page.locator("#harnessInputs").fill("input.txt")
        self.page.locator("#harnessWriteScope").fill("output.txt")
        self.page.locator("#harnessAcceptance").fill("A lead opens output.txt and verifies the expected line")
        self.page.locator("#harnessArtifact").fill("output.txt")
        self.page.locator("#harnessStop").fill("Stop if the file is outside the allowed scope")
        for selector, value in (("#harnessEstimateDirect", "20"), ("#harnessEstimateBriefing", "1"), ("#harnessEstimateExecution", "4"), ("#harnessEstimateVerification", "2"), ("#harnessEstimateRework", "1")):
            self.page.locator(selector).fill(value)
        self.page.get_by_role("button", name="Plan package").click()
        expect(self.page.get_by_text("Ready to launch", exact=True)).to_be_visible()
        expect(self.page.locator("#modelSelect")).to_have_value("gpt-5.6-sol")
        expect(self.page.locator("#reasoningSelect")).to_have_value("high")

    def test_delegate_review_retry_and_reload(self):
        self.plan()
        expect(self.page.locator(".harness-task").get_by_text("Delegated worker", exact=False)).to_be_visible()
        expect(self.page.locator(".harness-task")).to_contain_text("Implement <the bounded> fixture change")
        expect(self.page.locator(".harness-task")).to_contain_text("Acceptance (independently specified)")
        expect(self.page.locator(".harness-task")).to_contain_text("A lead opens output.txt and verifies the expected line")
        expect(self.page.locator(".harness-task")).to_contain_text("Prospective estimates (effort_points · estimated)")
        expect(self.page.locator(".harness-task")).to_contain_text("direct: 20")
        contract = self.page.locator(".harness-contract").first
        contract.locator("summary").click()
        self.assertFalse(contract.evaluate("node => node.open"))
        estimates = self.page.locator(".harness-estimates-summary")
        estimates.locator("summary").click()
        self.assertFalse(estimates.evaluate("node => node.open"))
        self.page.locator("#harnessClose").click()
        self.page.get_by_role("button", name="Harness").click()
        self.assertFalse(self.page.locator(".harness-contract").first.evaluate("node => node.open"))
        self.assertFalse(self.page.locator(".harness-estimates-summary").evaluate("node => node.open"))
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_role("button", name="Harness").click()
        expect(self.page.locator(".harness-contract").first).to_contain_text("output.txt")
        expect(self.page.locator(".harness-contract").first).to_contain_text("A lead opens output.txt and verifies the expected line")
        self.page.get_by_role("button", name="Launch").click()
        expect(self.page.get_by_role("button", name="Return to parent")).to_be_visible(timeout=5_000)
        expect(self.page.get_by_role("textbox", name="Message")).to_be_disabled()
        self.page.get_by_role("button", name="Return to parent").click()
        expect(self.page.get_by_text("Ready for review", exact=True)).to_be_visible(timeout=5_000)
        self.page.locator("[data-review-artifact]").fill("output.txt")
        self.page.locator("[data-review-evidence]").fill("Lead found the expected line missing")
        self.page.locator("[data-review-seconds]").fill("6")
        self.page.locator("[data-rework-seconds]").fill("3")
        self.page.locator("[data-review-effort-source]").select_option("measured")
        self.page.get_by_role("button", name="Reject").click()
        expect(self.page.get_by_text("Needs retry", exact=True)).to_be_visible()
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_role("button", name="Harness").click()
        expect(self.page.get_by_text("Needs retry", exact=True)).to_be_visible(timeout=5_000)
        expect(self.page.locator(".harness-attempt").first).to_contain_text("Review: 6 seconds (measured)")
        expect(self.page.locator(".harness-attempt").first).to_contain_text("Rework: 3 seconds (measured)")
        self.page.locator("[data-retry-evidence]").fill("The artifact missed the required line")
        retry_evidence = self.page.locator("[data-retry-evidence]")
        retry_evidence.focus()
        self.page.evaluate("renderHarnessTasks()")
        expect(retry_evidence).to_have_value("The artifact missed the required line")
        self.assertEqual(self.page.evaluate("document.activeElement?.dataset.retryEvidence"), retry_evidence.get_attribute("data-retry-evidence"))
        self.page.locator("[data-retry-task]").fill("Implement revised bounded fixture change")
        self.page.get_by_role("button", name="Plan retry").click()
        expect(self.page.get_by_text("Ready to launch", exact=True)).to_be_visible()
        expect(self.page.locator(".harness-task")).to_contain_text("gpt-5.6-luna")
        expect(self.page.locator(".harness-task")).to_contain_text("Implement revised bounded fixture change")
        expect(self.page.locator(".harness-attempt").first).to_contain_text("Implement <the bounded> fixture change")
        self.page.get_by_role("button", name="Launch").click()
        expect(self.page.get_by_role("button", name="Return to parent")).to_be_visible(timeout=5_000)
        self.page.get_by_role("button", name="Return to parent").click()
        expect(self.page.get_by_text("Ready for review", exact=True)).to_be_visible(timeout=5_000)
        expect(self.page.locator(".harness-attempt")).to_have_count(2)
        expect(self.page.locator(".harness-attempt").first).to_contain_text("Lead found the expected line missing")
        (self.fixture.project / "output.txt").write_text("expected line\n")
        self.page.locator("[data-review-evidence]").fill("Independent comparison found the expected line")
        self.page.locator("[data-review-seconds]").fill("8")
        self.page.get_by_role("button", name="Accept", exact=True).click()
        expect(self.page.get_by_text("Accepted", exact=True)).to_be_visible()
        self.page.reload(wait_until="domcontentloaded")
        self.page.get_by_role("button", name="Harness").click()
        expect(self.page.get_by_text("Accepted", exact=True)).to_be_visible()
        expect(self.page.locator(".harness-attempt").last).to_contain_text("Independent comparison found the expected line")
        expect(self.page.locator(".harness-attempt").last).to_contain_text("Review: 8 seconds (estimated)")

    def test_unchanged_retry_uses_terra_escalation(self):
        self.plan()
        self.page.get_by_role("button", name="Launch").click()
        expect(self.page.get_by_role("button", name="Return to parent")).to_be_visible(timeout=5_000)
        self.page.get_by_role("button", name="Return to parent").click()
        expect(self.page.get_by_text("Ready for review", exact=True)).to_be_visible(timeout=5_000)
        self.page.locator("[data-review-artifact]").fill("output.txt")
        self.page.locator("[data-review-evidence]").fill("The expected line is missing")
        self.page.get_by_role("button", name="Reject").click()
        expect(self.page.get_by_text("Needs retry", exact=True)).to_be_visible()
        self.page.locator("[data-retry-evidence]").fill("Escalate after the missing line")
        self.page.get_by_role("button", name="Plan retry").click()
        expect(self.page.get_by_text("Ready to launch", exact=True)).to_be_visible()
        expect(self.page.locator(".harness-task")).to_contain_text("gpt-5.6-terra")

    def test_three_rejections_exhaust_retry_with_new_package_cue(self):
        self.plan()
        for attempt in range(3):
            self.page.get_by_role("button", name="Launch").click()
            expect(self.page.get_by_role("button", name="Return to parent")).to_be_visible(timeout=5_000)
            self.page.get_by_role("button", name="Return to parent").click()
            expect(self.page.get_by_text("Ready for review", exact=True)).to_be_visible(timeout=5_000)
            self.page.locator("[data-review-artifact]").fill("output.txt")
            self.page.locator("[data-review-evidence]").fill(f"Attempt {attempt + 1} still misses the expected line")
            self.page.get_by_role("button", name="Reject").click()
            expect(self.page.get_by_text("Needs retry", exact=True)).to_be_visible()
            if attempt < 2:
                self.page.locator("[data-retry-evidence]").fill("The expected line is still missing")
                self.page.get_by_role("button", name="Plan retry").click()
                expect(self.page.get_by_text("Ready to launch", exact=True)).to_be_visible()
        expect(self.page.get_by_text("Attempt limit reached: define a new approach or package.", exact=True)).to_be_visible()
        self.assertFalse(self.page.get_by_role("button", name="Plan retry").is_visible())

    def test_harness_dialog_fits_desktop_and_narrow_viewports(self):
        self.page.get_by_role("button", name="Harness").click()
        for width in (1280, 760, 390):
            self.page.set_viewport_size({"width": width, "height": 900})
            self.assertTrue(self.page.locator("#harnessDialog").evaluate(
                "node => node.scrollWidth <= node.clientWidth + 1"))
            self.assertTrue(self.page.locator("#harnessForm").evaluate(
                "node => node.scrollWidth <= node.clientWidth + 1"))

    def test_trivial_read_only_contract_plans_directly(self):
        self.page.get_by_role("button", name="Harness").click()
        self.page.locator("#harnessPreset").select_option("sol-luna")
        self.page.locator("#harnessTask").fill("Read the fixture metadata")
        self.page.locator("#harnessCategory").select_option("trivial")
        self.page.locator("#harnessAcceptance").fill("A lead reads the reported metadata")
        self.page.locator("#harnessArtifact").fill("README.md")
        self.page.locator("#harnessStop").fill("Stop if metadata is unavailable")
        self.page.get_by_role("button", name="Plan package").click()
        expect(self.page.get_by_text("Ready to launch", exact=True)).to_be_visible()
        expect(self.page.locator(".harness-task")).to_contain_text("direct")


if __name__ == "__main__":
    unittest.main()
