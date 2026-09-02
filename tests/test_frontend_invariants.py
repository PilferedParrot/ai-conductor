"""Source-level checks for the browser UI's user-visible invariants.

The project does not ship a JavaScript/browser test harness, so these checks keep
the important UI contracts executable without requiring a browser dependency.
"""

import re
import unittest
from pathlib import Path


ASSET_DIR = Path(__file__).parents[1] / "pilferedparrot" / "web_assets"


def _function_body(source, name):
    """Return a named JavaScript function body, tolerating formatting changes."""
    match = re.search(
        rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^{{}}]*\)\s*\{{", source,
    )
    if not match:
        raise AssertionError(f"function {name} was not found")
    start = match.end()
    depth = 1
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index]
    raise AssertionError(f"function {name} has no closing brace")


class FrontendInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        cls.app_css = (ASSET_DIR / "app.css").read_text(encoding="utf-8")
        cls.index_html = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        cls.chat_js = (ASSET_DIR / "chat.js").read_text(encoding="utf-8")
        cls.chat_html = (ASSET_DIR / "chat.html").read_text(encoding="utf-8")

    def test_codex_budget_ui_selects_only_weekly_window(self):
        weekly_window = _function_body(self.app_js, "codexWeeklyWindow")
        providers = _function_body(self.app_js, "renderProviders")

        # The browser settings window exposes the weekly allowance. Keep the
        # filter tied to the duration/label rather than a fragile whole string.
        self.assertRegex(
            weekly_window.lower(),
            r"filter[\s\S]*(?:10080|weekly)",
            "codexWeeklyWindow must retain only the weekly allowance",
        )
        self.assertNotRegex(
            providers.lower(),
            r"5[ -]?hour|primary|secondary",
            "renderProviders must not present the other rate-limit bucket",
        )
        self.assertRegex(providers, r"codexWeeklyWindow")

    def test_sidebar_tracks_codex_but_not_qwen(self):
        providers = _function_body(self.app_js, "renderProviders")
        self.assertIn("OpenAI Codex", providers)
        self.assertNotRegex(providers.lower(), r"qwen")

    def test_work_log_scroll_position_is_preserved_across_message_renders(self):
        render_messages = _function_body(self.app_js, "renderMessages")
        capture = _function_body(self.app_js, "captureWorkScroll")
        restore = _function_body(self.app_js, "restoreWorkScroll")
        self.assertRegex(render_messages, r"work-items|work-log")

        # A rerender replaces innerHTML. The implementation must read the
        # existing work-log scrollTop before that replacement and write it back
        # afterwards (usually keyed by message id).
        self.assertRegex(render_messages, r"captureWorkScroll")
        self.assertRegex(render_messages, r"restoreWorkScroll")
        self.assertRegex(capture, r"querySelectorAll[\s\S]*work-items")
        self.assertRegex(capture, r"scrollTop")
        self.assertRegex(restore, r"querySelectorAll[\s\S]*work-items")
        self.assertRegex(restore, r"scrollTop\s*=")

    def test_single_line_assistant_commands_have_terminal_action(self):
        markdown = _function_body(self.app_js, "markdown")
        run_command = _function_body(self.app_js, "runTerminalCommand")
        self.assertRegex(markdown, r"data-run-command")
        self.assertRegex(markdown, r"!code\.includes\(\"\\n\"\)")
        self.assertRegex(run_command, r"/api/chats/.*?/terminal")
        self.assertRegex(run_command, r"message_id")
        self.assertRegex(run_command, r"block_index")
        self.assertRegex(run_command, r"confirm\(")

    def test_work_session_has_no_embedded_chat_pane(self):
        self.assertIn('id="sidebar"', self.index_html)
        self.assertIn('class="main"', self.index_html)
        self.assertIn('id="openChat"', self.index_html)
        self.assertNotIn('id="chatPane"', self.index_html)
        self.assertNotIn('id="chatHistoryList"', self.index_html)
        self.assertNotIn('id="chatContext"', self.index_html)
        self.assertNotIn('class="chat-pane"', self.index_html)
        self.assertRegex(self.app_css, r"\.shell\s*\{[^}]*grid-template-columns:[^;}]*minmax\(0,\s*1fr\)")
        self.assertIn("Restart PilferedParrot", self.app_js)

    def test_standalone_chat_window_is_bisected_and_model_selectable(self):
        self.assertIn('id="chatThreadTitle">Chat</div>', self.chat_html)
        self.assertIn('id="chatHistoryList"', self.chat_html)
        self.assertIn('id="resetChat"', self.chat_html)
        self.assertIn('id="chatContext"', self.chat_html)
        self.assertIn('id="chatMessages"', self.chat_html)
        self.assertIn('id="chatModelSelect"', self.chat_html)
        self.assertIn('gpt-5.6-terra', self.chat_html)
        self.assertIn('gpt-5.6-luna', self.chat_html)
        self.assertIn('class="chat-window-sidebar"', self.chat_html)
        self.assertIn('class="chat-window-conversation"', self.chat_html)
        self.assertIn('/api/chat/messages', self.chat_js)
        self.assertIn('JSON.stringify({ content, model })', self.chat_js)
        self.assertIn('state.draftModel', self.chat_js)
        self.assertIn('/api/chat/window', self.app_js)
        self.assertIn('id="sidebarResizer"', self.index_html)
        self.assertIn('role="separator"', self.index_html)
        self.assertRegex(self.app_css, r"--sidebar-width\s*:\s*286px")
        self.assertIn('localStorage.setItem(PANE_WIDTHS_KEY', self.app_js)
        self.assertIn('setupPaneResizer("#sidebarResizer", "sidebar")', self.app_js)

    def test_chat_popup_starts_as_a_usable_partial_window(self):
        opener = _function_body(self.app_js, "openChatWindow")
        self.assertRegex(
            opener,
            r"screen\.availWidth\s*\*\s*screen\.availHeight\)\s*/\s*6",
        )
        self.assertRegex(opener, r"aspectRatio\s*=\s*16\s*/\s*9")
        self.assertRegex(opener, r"Math\.sqrt\(targetArea\s*\*\s*aspectRatio\)")
        self.assertRegex(opener, r"Math\.sqrt\(targetArea\s*/\s*aspectRatio\)")
        self.assertRegex(opener, r"Math\.max\(720,")
        self.assertRegex(opener, r"Math\.max\(480,")
        self.assertIn("screen.availWidth - 56", opener)
        self.assertIn("screen.availHeight - 84", opener)
        self.assertIn("JSON.stringify({ width, height, left, top })", opener)

    def test_chat_window_is_launched_outside_the_maximized_main_browser(self):
        opener = _function_body(self.app_js, "openChatWindow")
        self.assertIn('api("/api/chat/window"', opener)
        self.assertNotIn("window.open", opener)
        self.assertNotIn("resizeTo", opener)

    def test_chat_model_picker_stays_operable_and_switches_via_new_chat(self):
        render = _function_body(self.chat_js, "render")
        self.assertRegex(
            render,
            r"modelSelect\.disabled\s*=\s*archived\s*\|\|\s*chatRunning\(\)",
            "a populated current chat must not permanently disable its model picker",
        )
        self.assertIn(
            '$("#chatModelSelect").addEventListener("change", selectChatModel)',
            self.chat_js,
        )
        body = _function_body(self.chat_js, "selectChatModel")
        self.assertIn("resetChat", body)
        self.assertIn("state.draftModel", body)


    def test_pilferedparrot_branding_and_company_assets_are_wired(self):
        self.assertIn("<title>PilferedParrot</title>", self.index_html)
        self.assertIn('aria-label="PilferedParrot home"', self.index_html)
        self.assertIn('/pilferedparrot-icon.png', self.index_html)
        self.assertIn('/company-logo-dark.png', self.index_html)
        self.assertTrue((ASSET_DIR / "pilferedparrot-icon.png").is_file())
        self.assertTrue((ASSET_DIR / "company-logo.png").is_file())
        self.assertTrue((ASSET_DIR / "company-logo-dark.png").is_file())
        self.assertNotIn(">Conductor<", self.index_html)

    def test_session_history_and_chat_history_are_separate_surfaces(self):
        self.assertIn('id="chatList"', self.index_html)
        self.assertIn('Session history', self.index_html)
        self.assertIn('aria-label="Session history"', self.index_html)
        self.assertIn('id="technicalContext"', self.index_html)
        self.assertNotIn('Technical activity', self.index_html)
        self.assertNotIn('Work activity', self.index_html)
        self.assertNotIn('Technical conversation history', self.index_html)
        self.assertIn('renderChats()', self.app_js)
        self.assertIn('renderHistory()', self.chat_js)
        self.assertIn('Chat history', self.chat_html)

    def test_starting_a_new_chat_has_no_success_notification(self):
        reset = _function_body(self.chat_js, "resetChat")
        self.assertNotIn("Started a new Chat", reset)
        self.assertNotIn("Previous Chat archived safely", reset)
        self.assertIn("catch (error) { toast(error.message); }", reset)

    def test_new_work_session_and_chat_actions_are_independent(self):
        self.assertIn('id="newWorkSession"', self.index_html)
        self.assertIn('New work session', self.index_html)
        technical = self.index_html.split('<main class="main"', 1)[1].split("</main>", 1)[0]
        self.assertIn('id="newWorkSession"', technical)
        self.assertNotIn('id="newParrotChat"', self.index_html)
        self.assertNotIn('id="newChat"', self.index_html)
        self.assertIn(
            '$("#newWorkSession").addEventListener("click", () => createChat()',
            self.app_js,
        )
        create = _function_body(self.app_js, "createChat")
        self.assertIn("/api/chats", create)
        self.assertNotIn("/api/chat/", create)

        self.assertNotIn('id="resetChat"', self.index_html)
        self.assertNotIn('/api/chat/reset', self.app_js)

    def test_chat_actions_are_visibly_labeled(self):
        self.assertRegex(
            self.chat_html,
            r'id="resetChat"[^>]*>[\s\S]*?New chat[\s\S]*?</button>',
        )
        self.assertIn('class="chat-new-chat"', self.chat_html)
        self.assertRegex(
            self.index_html,
            r'id="openChat"[^>]*>[\s\S]*?Open chat[\s\S]*?</button>',
        )
        self.assertIn('class="open-chat-button"', self.index_html)

    def test_unneeded_delete_and_composer_labels_are_absent(self):
        self.assertNotIn('id="deleteChat"', self.index_html)
        self.assertNotIn('function deleteChat', self.app_js)
        self.assertNotIn('id="chatModelNote"', self.chat_html)
        self.assertNotIn('chat-model-note', self.app_css)
        self.assertRegex(
            self.app_css,
            r"\.chat-composer-bar\s*\{[^}]*justify-content:\s*flex-end",
        )

    def test_context_values_share_a_baseline_group(self):
        context_markup = _function_body(self.app_js, "contextUsageMarkup")
        self.assertIn('class="context-usage-value"', context_markup)
        self.assertRegex(
            self.app_css,
            r"\.context-usage-value\s*\{[^}]*align-items:\s*baseline",
        )
        self.assertRegex(
            self.app_css,
            r"\.provider-card-head\s*\{[^}]*align-items:\s*baseline",
        )

    def test_narrow_technical_panes_reduce_header_density(self):
        self.assertRegex(self.app_css, r"\.main\s*\{[^}]*container-type:\s*inline-size")
        self.assertRegex(self.app_css, r"@container\s*\(max-width:\s*920px\)")
        self.assertRegex(self.app_css, r"@container\s*\(max-width:\s*700px\)")

    def test_practical_context_limit_indicators_are_wired(self):
        self.assertIn('id="technicalContext"', self.index_html)
        self.assertIn("context_status", _function_body(self.app_js, "renderChats"))
        self.assertIn('id="chatContext"', self.chat_html)
        self.assertIn("context_status", self.chat_js)
        self.assertIn("practical limit", self.app_js.lower())

    def test_main_header_shows_only_session_name_and_authorized_folder(self):
        self.assertIn('id="chatTitle"', self.index_html)
        self.assertIn('id="projectButton"', self.index_html)
        self.assertNotIn('id="activeModel"', self.index_html)
        self.assertNotIn('class="model-badge"', self.index_html)
        self.assertNotIn('Powered by GPT-5.6', self.index_html)
        self.assertNotIn('work activity', self.index_html.lower())

    def test_work_session_can_select_sol_independently(self):
        model_picker = _function_body(self.app_js, "renderModelSelect")
        self.assertIn('gpt-5.6-sol', model_picker)
        self.assertNotIn('gpt-5.6-terra', model_picker)
        self.assertNotIn('gpt-5.6-luna', model_picker)

    def test_launcher_opens_main_app_maximized(self):
        launcher = (ASSET_DIR.parents[1] / "bin" / "pilferedparrot-app-browser").read_text(encoding="utf-8")
        self.assertIn("--start-maximized", launcher)

    def test_launcher_tracks_browser_and_notifies_server_after_close(self):
        launcher = (ASSET_DIR.parents[1] / "bin" / "pilferedparrot-app-browser").read_text(encoding="utf-8")
        browser_launch = launcher.index('"$browser" \\\n')
        browser_status = launcher.index("browser_status=$?", browser_launch)
        close_notification = launcher.index(
            '"$SCRIPT_DIR/pilferedparrot" gui --window-closed "$url"', browser_status,
        )
        self.assertNotIn('exec "$browser"', launcher)
        self.assertIn("--disable-background-mode", launcher)
        self.assertIn('if [ "$browser_lifetime" -ge 2 ]', launcher)
        self.assertLess(browser_launch, browser_status)
        self.assertLess(browser_status, close_notification)
        self.assertIn('exit "$browser_status"', launcher[close_notification:])
        self.assertNotIn("--window-closed", launcher[:close_notification])

    def test_main_window_close_notifies_server_even_if_chat_window_remains(self):
        init = _function_body(self.app_js, "init")
        self.assertIn('/api/window/open', init)
        self.assertIn('window.addEventListener("pagehide"', self.app_js)
        self.assertIn('/api/window/close', self.app_js)
        self.assertIn('keepalive: true', self.app_js)

    def test_chat_assets_are_served_as_separate_documents(self):
        self.assertIn('<script src="/app.js"></script>', self.index_html)
        self.assertIn('<script src="/chat.js"></script>', self.chat_html)
        self.assertNotIn('<script src="/chat.js"></script>', self.index_html)

    def test_css_has_no_font_size_below_twelve_point(self):
        css = re.sub(r"/\*.*?\*/", "", self.app_css, flags=re.DOTALL)
        declarations = re.findall(r"font-size\s*:\s*([^;}]*)", css, flags=re.I)
        # Also cover a numeric size embedded in the CSS font shorthand.
        declarations += re.findall(r"(?:^|[;{])\s*font\s*:\s*([^;}]*)", css, flags=re.I)
        self.assertTrue(declarations, "expected explicit font declarations in app.css")

        for declaration in declarations:
            for value, unit in re.findall(
                r"(-?(?:\d+(?:\.\d*)?|\.\d+))\s*(px|pt|em|rem|%)\b",
                declaration,
                flags=re.I,
            ):
                number = float(value)
                unit = unit.lower()
                if unit == "px":
                    self.assertGreaterEqual(number, 16, declaration)
                elif unit == "pt":
                    self.assertGreaterEqual(number, 12, declaration)
                elif unit in {"em", "rem", "%"}:
                    # Relative sizes below the inherited 16px/12pt baseline
                    # are smaller than the requested minimum.
                    self.assertGreaterEqual(number, 1 if unit != "%" else 100, declaration)


if __name__ == "__main__":
    unittest.main()
