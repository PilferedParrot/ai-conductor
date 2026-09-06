"""Headless end-to-end tests for the real PilferedParrot browser UI."""

from __future__ import annotations

import os
import re
import sys
import unittest
from urllib.parse import urlparse
from unittest.mock import patch

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
        self.page.on("pageerror", lambda error: self.page_errors.append(error))
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

    def test_shared_markdown_renderer_is_safe_identical_and_keeps_command_actions(self):
        formatted = (
            "# Shared heading\n"
            "Paragraph with a\nline break, *emphasis*, **strong**, `inline code`, "
            "[safe link](https://example.invalid/docs?q=one&x=two), "
            "[email](mailto:test@example.invalid), and "
            "[unsafe link](javascript:alert(1)).\n\n"
            "- alpha\n- beta\n\n"
            "1. first\n2. second\n\n"
            "> quoted response\n\n"
            "---\n\n"
            "| Name | Value |\n| :--- | ---: |\n| raw | <img src=https://outside.invalid/pixel> |\n\n"
            "```python\nprint('<safe>')\n```\n\n"
            "Unmatched fence remains visible:\n```sh\necho never-runnable"
        )
        commands = (
            "```python\nprint('skip')\n```\n"
            "```bash\necho first\n```\n"
            "```sh\none\ntwo\n```\n"
            "```\necho second\n```"
            + ("\n```powershell\nWrite-Output 'accepted on Windows'\n```"
               if sys.platform == "win32" else "")
        )
        with self.fixture.app.store.lock:
            work_chat = self.fixture.app.store.data["chats"][0]
            work_chat["messages"] = [
                {
                    "id": "work-formatted", "role": "assistant", "content": formatted,
                    "provider": "codex", "model": "fake-small",
                },
                {
                    "id": "work-commands", "role": "assistant", "content": commands,
                    "provider": "codex", "model": "fake-small",
                },
                {"id": "user-command", "role": "user", "content": "```bash\necho no\n```"},
            ]
            self.fixture.app.store.data["chat"]["messages"] = [
                {"id": "chat-formatted", "role": "assistant", "content": formatted},
            ]
            self.fixture.app.store.save()

        self.page.reload(wait_until="domcontentloaded")
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled()
        work_formatted = self.page.locator("article.message.assistant .message-content").first
        expect(work_formatted.get_by_role("heading", name="Shared heading")).to_be_visible()
        expect(work_formatted.locator("ul li")).to_have_count(2)
        expect(work_formatted.locator("ol li")).to_have_count(2)
        expect(work_formatted.locator("blockquote")).to_contain_text("quoted response")
        expect(work_formatted.locator("table tbody tr")).to_have_count(1)
        expect(work_formatted.locator(".code-block[data-language=python]")).to_be_visible()
        expect(work_formatted.locator("img, script, iframe, object")).to_have_count(0)
        expect(work_formatted).to_contain_text("<img src=https://outside.invalid/pixel>")
        expect(work_formatted).to_contain_text("```sh")
        expect(work_formatted.get_by_role("link")).to_have_count(2)
        safe_link = work_formatted.get_by_role("link", name="safe link")
        expect(safe_link).to_have_attribute("target", "_blank")
        expect(safe_link).to_have_attribute("rel", "noopener noreferrer")
        expect(work_formatted.get_by_text("[unsafe link](javascript:alert(1))")).to_be_visible()

        chat_capability = self.fixture.app.issue_capability("chat", provider="codex")
        chat_page = self.context.new_page()
        chat_page.on("pageerror", lambda error: self.page_errors.append(error))
        chat_page.goto(
            f"{self.fixture.base_url}/chat#capability={chat_capability}&provider=codex",
            wait_until="domcontentloaded",
        )
        expect(chat_page.get_by_role("textbox", name="Message Chat")).to_be_enabled()
        chat_formatted = chat_page.locator("article.chat-message.assistant .chat-message-body").first
        expect(chat_formatted.get_by_role("heading", name="Shared heading")).to_be_visible()
        self.assertEqual(work_formatted.inner_html(), chat_formatted.inner_html())

        command_message = self.page.locator("article.message.assistant").nth(1)
        buttons = command_message.locator("[data-run-command]")
        expected_blocks = ["1", "3"]
        if sys.platform == "win32":
            expected_blocks.append("4")
        expect(buttons).to_have_count(len(expected_blocks))
        self.assertEqual(buttons.evaluate_all("nodes => nodes.map(node => node.dataset.blockIndex)"), expected_blocks)
        expect(self.page.locator("article.message.user [data-run-command]")).to_have_count(0)
        dialog = self.page.locator("#terminalDialog")
        if sys.platform == "win32":
            # Windows deliberately rejects Unix shell fences at the server
            # boundary, even though the markdown action remains visible.
            buttons.first.click()
            expect(dialog).to_be_visible()
            expect(dialog.locator("#terminalCommand")).to_have_text("echo first")
            with self.page.expect_response("**/terminal") as rejected_response, \
                 patch("pilferedparrot.web.subprocess.Popen") as popen:
                dialog.locator("#confirmTerminal").click()
            self.assertEqual(rejected_response.value.status, 400)
            expect(self.page.locator("#toast")).to_contain_text("requires a Unix shell")
            popen.assert_not_called()
            dialog.get_by_role("button", name="Cancel").click()

            # A single stored PowerShell command is accepted; Popen stays
            # mocked so this browser test never opens a real terminal.
            buttons.nth(2).click()
            expect(dialog.locator("#terminalCommand")).to_have_text(
                "Write-Output 'accepted on Windows'",
            )
            with self.page.expect_response("**/terminal") as accepted_response, \
                 patch("pilferedparrot.web.subprocess.Popen") as popen:
                dialog.locator("#confirmTerminal").click()
            self.assertTrue(accepted_response.value.ok)
            expect(self.page.locator("#toast")).to_contain_text("Opened command in a terminal.")
            self.assertTrue(popen.called)
        else:
            buttons.first.click()
            expect(dialog).to_be_visible()
            expect(dialog.locator("#terminalCommand")).to_have_text("echo first")
            with patch("pilferedparrot.web._terminal_argv", return_value=["terminal"]), \
                 patch("pilferedparrot.web.subprocess.Popen") as popen:
                dialog.locator("#confirmTerminal").click()
                expect(self.page.locator("#toast")).to_contain_text("Opened command in a terminal.")
            self.assertTrue(popen.called)

    def test_markdown_edge_cases_do_not_create_quoted_or_malformed_actions(self):
        content = (
            "This | is table-like prose\n"
            "without | a delimiter row\n\n"
            "| Literal | Result |\n"
            "| --- | --- |\n"
            "| a \\| b | preserved |\n\n"
            "> outer quote\n"
            "> > ```bash\n"
            "> > echo quoted-only\n"
            "> > ```\n\n"
            "inline ```bash echo malformed``` remains text\n\n"
            "```bash\necho runnable\n```"
        )
        with self.fixture.app.store.lock:
            work_chat = self.fixture.app.store.data["chats"][0]
            work_chat["messages"] = [{
                "id": "edge-cases", "role": "assistant", "content": content,
                "provider": "codex", "model": "fake-small",
            }]
            self.fixture.app.store.save()

        self.page.reload(wait_until="domcontentloaded")
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled()
        rendered = self.page.locator("article.message.assistant .message-content")
        expect(rendered).to_contain_text(
            re.compile(r"This \| is table-like prose\s*without \| a delimiter row"),
        )
        expect(rendered.locator("table")).to_have_count(1)
        expect(rendered.locator("table tbody tr")).to_contain_text("a | b")
        expect(rendered.locator("blockquote blockquote")).to_contain_text("echo quoted-only")
        expect(rendered).to_contain_text("inline ```bash echo malformed``` remains text")
        actions = rendered.locator("[data-run-command]")
        expect(actions).to_have_count(1)
        self.assertEqual(actions.get_attribute("data-block-index"), "0")
        actions.click()
        expect(self.page.locator("#terminalCommand")).to_have_text("echo runnable")


@unittest.skipUnless(sync_playwright, "install requirements-browser.txt to run Playwright")
class ClaudeUsageBrowserEndToEndTests(unittest.TestCase):
    """Exercise the provider-owned unavailable-usage contract in a real page."""

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
        self.fixture = PilferedParrotBrowserFixture(include_claude=True)
        self.addCleanup(self.fixture.stop)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.goto(self.fixture.browser_url, wait_until="domcontentloaded")
        expect(self.page.get_by_role("textbox", name="Message")).to_be_enabled(timeout=5_000)

    def test_signed_in_claude_shows_note_without_allowance_widgets(self):
        expect(self.page.locator("#providerList")).to_contain_text(
            "Claude allowance is unavailable in PilferedParrot.",
        )
        expect(self.page.locator("#providerList .usage-unavailable-note")).to_have_count(1)
        expect(self.page.locator("#providerList [role=progressbar]")).to_have_count(0)
        expect(self.page.locator("#providerList .allowance-reset")).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
