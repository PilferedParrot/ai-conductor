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


def _container_query_bodies(source, name):
    """Return the bodies of named CSS container queries, tolerating formatting."""
    bodies = []
    pattern = re.compile(rf"@container\s+{re.escape(name)}\s*\([^)]*\)\s*\{{")
    for match in pattern.finditer(source):
        depth = 1
        for index in range(match.end(), len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(source[match.end():index])
                    break
    return bodies


class FrontendInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        cls.app_css = (ASSET_DIR / "app.css").read_text(encoding="utf-8")
        cls.index_html = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        cls.chat_js = (ASSET_DIR / "chat.js").read_text(encoding="utf-8")
        cls.chat_html = (ASSET_DIR / "chat.html").read_text(encoding="utf-8")
        cls.markdown_js = (ASSET_DIR / "markdown.js").read_text(encoding="utf-8")

    def test_pick_project_launch_asks_before_creating_a_chat(self):
        """A window opened without a usable folder must ask, not start a doomed chat."""
        body = _function_body(self.app_js, "init")
        self.assertIn("fragmentPickProject", body)
        self.assertIn("openProjectDialog(true)", body)
        self.assertIn('id="projectNotice"', self.index_html)

    def test_pick_project_launch_keeps_prompt_open_when_chat_creation_fails(self):
        submit = self.app_js.index('$("#projectForm").addEventListener("submit"')
        create = self.app_js.index("await createChat(model)", submit)
        clear = self.app_js.index("pendingLaunchModel = null", submit)
        close = self.app_js.index('$("#projectDialog").close()', submit)
        self.assertLess(create, clear)
        self.assertLess(clear, close)
        self.assertIn("catch (error)", self.app_js[submit:close])
        self.assertIn("projectSubmitPending", self.app_js[submit:close])

    def test_project_folder_can_be_selected_with_a_native_chooser(self):
        self.assertRegex(
            self.index_html,
            r'id="browseProject"[^>]*>Browse…</button>',
        )
        self.assertIn('api("/api/project/folder"', self.app_js)
        self.assertIn('$("#projectInput").value = selected.path', self.app_js)

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
        usage_windows = _function_body(self.app_js, "providerUsageWindows")
        self.assertRegex(usage_windows, r"codexWeeklyWindow")

    def test_sidebar_tracks_only_the_window_provider(self):
        providers = _function_body(self.app_js, "renderProviders")
        self.assertIn("state.windowProvider", providers)
        self.assertIn("providerLabel(provider)", providers)

    def test_claude_sidebar_explains_unsupported_usage_without_allowance_widgets(self):
        usage_windows = _function_body(self.app_js, "providerUsageWindows")
        unavailable = _function_body(self.app_js, "providerUsageUnavailableMarkup")
        providers = _function_body(self.app_js, "renderProviders")
        self.assertNotIn('provider === "claude"', usage_windows)
        self.assertNotIn("claudeSidebarWindows", self.app_js)
        self.assertIn('["unavailable", "unsupported"]', unavailable)
        self.assertIn("!budget?.usage_note", unavailable)
        self.assertIn("budget.usage_note", unavailable)
        self.assertNotIn("Live allowance unavailable", unavailable)
        self.assertIn('<p class="usage-unavailable-note">', unavailable)
        self.assertNotIn("allowanceMarkup", unavailable)
        self.assertNotIn("progressbar", unavailable)
        self.assertNotIn("Resets", unavailable)
        self.assertRegex(providers, r'model\s*===\s*"Provider-selected model"\s*\?\s*""')
        self.assertIn("allowanceMarkup", providers)
        self.assertIn("providerUsageUnavailableMarkup(budget)", providers)
        self.assertIn("usageUnavailable", providers)
        self.assertIn(".usage-unavailable-note", self.app_css)

    def test_unsupported_usage_note_is_provider_neutral_and_legacy_windows_are_not_current(self):
        unavailable = _function_body(self.app_js, "providerUsageUnavailableMarkup")
        usage_windows = _function_body(self.app_js, "providerUsageWindows")
        self.assertNotIn("claude", unavailable.lower())
        self.assertNotRegex(unavailable, r'provider\s*[!=]==?')
        self.assertIn('["unavailable", "unsupported"]', unavailable)
        self.assertIn("usage_note", unavailable)
        # Legacy Claude allowance windows may remain in persisted/API state, but
        # only the explicitly supported provider window is current UI quota.
        self.assertRegex(usage_windows, r'provider\s*===\s*"codex"')
        self.assertNotRegex(usage_windows, r'provider\s*===\s*"claude"')

    def test_allowance_resets_are_compact_with_exact_local_time_available(self):
        reset_time = _function_body(self.app_js, "allowanceResetTime")
        markup = _function_body(self.app_js, "allowanceMarkup")
        self.assertIn("Date.now()", reset_time)
        self.assertIn("Resets in", reset_time)
        self.assertIn("toLocaleString", reset_time)
        self.assertIn('title="${escapeHtml(reset.exact)}"', markup)
        self.assertIn('datetime="${escapeHtml(reset.datetime)}"', markup)
        self.assertIn('aria-label="${escapeHtml(reset.exact)}"', markup)
        self.assertRegex(
            self.app_css,
            r"\.allowance-meter\s*\{[^}]*display:\s*flex[^}]*align-items:\s*center",
        )

    def test_provider_ui_separates_authentication_and_reachability(self):
        providers = _function_body(self.app_js, "renderProviders")
        self.assertIn("auth_status", providers)
        self.assertIn("reachability", providers)
        self.assertIn("Checking…", providers)
        self.assertIn("Status unavailable", providers)
        self.assertNotIn('"Auth unknown"', providers)
        self.assertNotIn('"Reachability unknown"', providers)
        self.assertNotIn('"Connected"', providers)

    def test_provider_refresh_spins_clockwise_and_healthy_status_is_concise(self):
        providers = _function_body(self.app_js, "renderProviders")
        reachability = _function_body(self.app_js, "providerReachabilityText")
        self.assertRegex(
            reachability,
            r'reachability\s*===\s*"reachable"\)\s*return\s*""',
        )
        self.assertNotIn('reachable: "Reachable"', self.app_js)
        self.assertIn('classList.add("refreshing")', self.app_js)
        self.assertRegex(
            self.app_css,
            r'@keyframes\s+provider-refresh-spin\s*\{[^}]*rotate\(360deg\)',
        )

    def test_qwen_auto_start_is_presented_as_ready_on_demand(self):
        reachability = _function_body(self.app_js, "providerReachabilityText")
        providers = _function_body(self.app_js, "renderProviders")
        connections = _function_body(self.app_js, "renderProviderConnections")
        self.assertIn('provider === "qwen"', reachability)
        self.assertIn('budget?.available', reachability)
        self.assertIn('"Ready on demand"', reachability)
        self.assertIn("providerReachabilityText(provider, budget)", providers)
        self.assertIn("providerReachabilityText(provider, budget)", connections)

    def test_budget_refresh_runs_after_message_completion(self):
        poll = _function_body(self.app_js, "schedulePoll")
        send = _function_body(self.app_js, "sendMessage")
        self.assertIn("wasRunning", poll)
        self.assertIn("!anyRunning()", poll)
        self.assertIn("await refreshBudgets(false)", poll)
        self.assertIn("refreshBudgets(false)", send)

    def test_each_window_keeps_its_fragment_capability_across_reload(self):
        for source, scope in (
            (self.app_js, "dashboard"),
            (self.chat_js, "chat"),
        ):
            self.assertIn('fragment.get("capability")', source)
            self.assertIn(f'"pilferedparrot-{scope}-capability"', source)
            self.assertIn("sessionStorage.getItem(CAPABILITY_SESSION_KEY)", source)
            self.assertIn("sessionStorage.setItem(CAPABILITY_SESSION_KEY", source)
            self.assertIn('"X-PilferedParrot-Capability"', source)
            self.assertNotIn("csrf_token", source)

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
        render_messages = _function_body(self.app_js, "renderMessages")
        run_command = _function_body(self.app_js, "runTerminalCommand")
        confirm_command = _function_body(self.app_js, "confirmTerminalCommand")
        self.assertIn("data-run-command", self.markdown_js)
        self.assertRegex(self.markdown_js, r'code\.indexOf\(\"\\n\"\)\s*<\s*0')
        self.assertIn("commandTarget: assistant && message.id", render_messages)
        self.assertIn("shellLanguages: CODE_BLOCK_LANGUAGES", render_messages)
        self.assertIn('id="terminalDialog"', self.index_html)
        self.assertIn('id="confirmTerminal"', self.index_html)
        self.assertIn("▶", self.index_html)
        self.assertIn("sudo", self.index_html)
        self.assertRegex(run_command, r"terminalDialog.*showModal")
        self.assertNotRegex(run_command, r"confirm\(")
        self.assertRegex(confirm_command, r"/api/chats/.*?/terminal")
        self.assertRegex(confirm_command, r"message_id")
        self.assertRegex(confirm_command, r"block_index")

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
        self.assertIn('Loading provider models', self.chat_html)
        self.assertIn('model_catalog', self.chat_js)
        self.assertIn('class="chat-window-sidebar"', self.chat_html)
        self.assertIn('class="chat-window-conversation"', self.chat_html)
        self.assertIn('id="chatResizer"', self.chat_html)
        self.assertIn('aria-label="Resize chat sidebar"', self.chat_html)
        self.assertIn('aria-controls="chatWindowSidebar"', self.chat_html)
        self.assertIn('/api/chat/messages', self.chat_js)
        self.assertIn('reasoning_effort: state.chat?.reasoning_effort || null', self.chat_js)
        self.assertIn('state.draftModel', self.chat_js)
        self.assertIn('/api/chat/window', self.app_js)
        self.assertIn('id="sidebarResizer"', self.index_html)
        self.assertIn('role="separator"', self.index_html)
        self.assertIn('aria-controls="sidebar"', self.index_html)
        self.assertRegex(self.app_css, r"--sidebar-width\s*:\s*286px")
        self.assertIn('localStorage.setItem(PANE_WIDTHS_KEY', self.app_js)
        self.assertIn('setupPaneResizer("#sidebarResizer", "sidebar")', self.app_js)

    def test_standalone_chat_sidebar_resizer_supports_persistence_and_keyboard(self):
        self.assertIn('const CHAT_PANE_WIDTHS_KEY = "pilferedparrot-pane-widths";', self.chat_js)
        self.assertIn('setupChatSidebarResizer()', self.chat_js)
        self.assertIn('["ArrowLeft", "ArrowRight", "Home", "End"]', self.chat_js)
        self.assertIn('localStorage.setItem(CHAT_PANE_WIDTHS_KEY', self.chat_js)
        self.assertIn('CHAT_CONVERSATION_MIN_WIDTH', self.chat_js)
        self.assertRegex(self.app_css, r"\.chat-window\s*\{[^}]*grid-template-columns:[^;}]*var\(--chat-sidebar-width")
        self.assertIn(".chat-sidebar-resizer { display: none; }", self.app_css)

    def test_chat_history_precedes_controls_and_has_reserved_height(self):
        history = self.chat_html.find('class="history-group chat-window-history"')
        model = self.chat_html.find('id="chatModelSelect"')
        context = self.chat_html.find('class="chat-window-context sidebar-disclosure"')
        self.assertGreaterEqual(history, 0, "Chat history section is missing")
        self.assertGreaterEqual(model, 0, "Chat model controls are missing")
        self.assertGreaterEqual(context, 0, "Chat context controls are missing")
        self.assertLess(history, model, "Chat history must appear before model controls")
        self.assertLess(history, context, "Chat history must appear before context controls")

        rule = re.search(r"(?m)^\.chat-window-history\s*\{([^}]*)\}", self.app_css)
        self.assertIsNotNone(rule, "Chat history CSS rule is missing")
        minimum = re.search(r"\bmin-height\s*:\s*([0-9]+)(?:px)?\s*;", rule.group(1))
        self.assertIsNotNone(minimum, "Chat history needs a nonzero minimum height")
        self.assertGreater(int(minimum.group(1)), 0, "Chat history minimum height must be nonzero")

    def test_work_and_chat_sidebars_keep_history_overflow_local(self):
        for selector in ("sidebar", "chat-window-sidebar"):
            self.assertRegex(
                self.app_css,
                rf"\.{selector}\s*\{{[^}}]*overflow-y:\s*auto",
            )
        self.assertRegex(
            self.app_css,
            r"\.chat-list\s*\{[^}]*overflow-y:\s*auto",
        )
        self.assertRegex(
            self.app_css,
            r"\.technical-history\s+\.chat-list\s*\{[^}]*overflow-y:\s*auto",
        )
        self.assertRegex(
            self.app_css,
            r"\.chat-window-history\s*\{[^}]*overflow-y:\s*auto",
        )

    def test_chat_popup_always_starts_at_the_selected_dimensions(self):
        opener = _function_body(self.app_js, "openChatWindow")
        self.assertRegex(self.app_js, r"CHAT_WINDOW_WIDTH\s*=\s*871")
        self.assertRegex(self.app_js, r"CHAT_WINDOW_HEIGHT\s*=\s*376")
        self.assertRegex(opener, r"width\s*=\s*CHAT_WINDOW_WIDTH")
        self.assertRegex(opener, r"height\s*=\s*CHAT_WINDOW_HEIGHT")
        self.assertRegex(opener, r"JSON\.stringify\(\{[\s\S]*provider:[\s\S]*model:[\s\S]*width")

    def test_chat_window_is_launched_outside_the_maximized_main_browser(self):
        opener = _function_body(self.app_js, "openChatWindow")
        self.assertIn('api("/api/chat/window"', opener)
        self.assertNotIn("window.open", opener)
        self.assertNotIn("resizeTo", opener)

    def test_chat_model_picker_stays_operable_and_switches_via_new_chat(self):
        render = _function_body(self.chat_js, "render")
        self.assertRegex(
            render,
            r"modelSelect\.disabled\s*=\s*!state\.initialized\s*\|\|\s*archived\s*\|\|\s*chatRunning\(\)",
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

    def test_new_session_welcome_has_one_company_logo_and_no_shortcuts(self):
        welcome = re.search(
            r'<div class="welcome" id="welcome">([\s\S]*?)<div class="messages"',
            self.index_html,
        )
        self.assertIsNotNone(welcome)
        markup = welcome.group(1)
        self.assertEqual(markup.count('/company-logo-dark.png'), 1)
        self.assertNotIn('/pilferedparrot-icon.png', markup)
        self.assertNotIn('data-provider-choice', markup)
        self.assertNotIn('data-prompt', markup)
        self.assertNotIn('Debug failing tests', markup)

    def test_fork_prompt_is_friendly_and_uses_the_intended_frequency(self):
        chooser = _function_body(self.app_js, "choosePromptSuggestion")
        accepter = _function_body(self.app_js, "acceptPromptSuggestion")
        click_target = _function_body(self.app_js, "clickedAfterPromptSuggestion")

        self.assertIn('const DEFAULT_PROMPT_PLACEHOLDER = "Describe what you want done"', self.app_js)
        self.assertIn('placeholder="Describe what you want done"', self.index_html)
        self.assertIn(
            'const FORK_PROMPT_SUGGESTION = "Help me create my own version of the '
            'Pilfered Parrot interface, then ask me what I\'d like to change."',
            self.app_js,
        )
        self.assertIn("promptSuggestionSelections === 0", chooser)
        self.assertRegex(chooser, r"Math\.random\(\)\s*<\s*1\s*/\s*15")
        self.assertIn("promptSuggestionSelections += 1", chooser)
        self.assertRegex(
            chooser,
            r"showForkSuggestion\s*\?\s*FORK_PROMPT_SUGGESTION\s*:\s*DEFAULT_PROMPT_PLACEHOLDER",
        )
        self.assertRegex(
            chooser,
            r'promptSuggestion\s*=\s*placeholder\s*===\s*FORK_PROMPT_SUGGESTION\s*\?\s*placeholder\s*:\s*""',
        )
        self.assertIn('event.key === "ArrowRight"', self.app_js)
        self.assertRegex(accepter, r"value\.length[\s\S]*value\s*=\s*promptSuggestion")
        self.assertIn("setSelectionRange", accepter)
        self.assertIn("promptSuggestionEndRect", click_target)
        self.assertRegex(click_target, r"event\.clientX[\s\S]*event\.clientY")
        self.assertIn('addEventListener("click"', self.app_js)
        self.assertNotRegex(chooser, r"api\(|fetch\(")

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
        sidebar = self.index_html.split('<aside class="sidebar"', 1)[1].split("</aside>", 1)[0]
        self.assertIn('id="newWorkSession"', sidebar)
        self.assertNotIn('id="newParrotChat"', self.index_html)
        self.assertNotIn('id="newChat"', self.index_html)
        self.assertIn(
            '$("#newWorkSession").addEventListener("click", () => {',
            self.app_js,
        )
        self.assertIn('preferredModel(state.windowProvider)', self.app_js)
        create = _function_body(self.app_js, "createChat")
        self.assertIn("/api/chats", create)
        self.assertNotIn("/api/chat/", create)

    def test_new_work_session_sends_the_visible_default_choice(self):
        create = _function_body(self.app_js, "createChat")
        self.assertIn('$("#reasoningSelect").value || null : undefined', create)
        self.assertNotIn("draftReasoningEffort", create)

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
            r'id="openChat"[^>]*aria-label="Open Chat window"[^>]*>[\s\S]*?<span>Chat</span>[\s\S]*?</button>',
        )
        self.assertIn('class="open-chat-button"', self.index_html)

    def test_project_and_chat_controls_have_explicit_accessible_names(self):
        self.assertRegex(
            self.index_html,
            r'id="projectInput"[^>]*aria-label="Project folder"',
        )
        self.assertRegex(
            self.chat_html,
            r'id="resetChat"[^>]*aria-label="Start a new chat"',
        )

    def test_provider_menu_can_authenticate_and_open_another_window(self):
        self.assertIn('id="providerWindows"', self.index_html)
        provider_button = self.index_html.split(
            'id="providerWindows"', 1,
        )[1].split('</button>', 1)[0]
        self.assertNotIn("⋯", provider_button)
        self.assertIn("<svg", provider_button)
        self.assertIn('id="providerDialog"', self.index_html)
        self.assertIn('id="providerLogoutDialog"', self.index_html)
        self.assertNotIn('id="providerSelect"', self.index_html)
        self.assertIn('id="modelSelect"', self.index_html)
        self.assertIn('Choose a provider and model', self.index_html)
        connections = _function_body(self.app_js, "renderProviderConnections")
        self.assertNotIn('data-provider-current', connections)
        self.assertIn('data-provider-window', connections)
        self.assertIn('data-provider-login', connections)
        self.assertIn('data-provider-logout', connections)
        opener = _function_body(self.app_js, "openProviderWindow")
        self.assertIn('api("/api/provider/window"', opener)
        self.assertNotIn("window.open", opener)
        self.assertIn('JSON.stringify({ document_id: documentId })', self.app_js)
        self.assertIn('fragment.get("provider")', self.app_js)
        self.assertIn('fragment.get("window")', self.app_js)

    def test_provider_dashboard_exposes_models_and_safe_login_guidance(self):
        dialog = self.index_html.split('<dialog id="providerDialog">', 1)[1].split('</dialog>', 1)[0]
        self.assertIn("Provider dashboard", dialog)
        self.assertIn('id="refreshProviderDashboard"', dialog)
        connections = _function_body(self.app_js, "renderProviderConnections")
        self.assertIn('data-provider-model', connections)
        self.assertIn('data-provider-window', connections)
        self.assertIn('providerModelOptions(provider)', connections)
        self.assertIn('id="addProvider"', dialog)
        self.assertIn('provider_templates', self.app_js)
        self.assertIn('data-provider-remove', connections)
        self.assertIn('Remove provider', connections)
        draft = _function_body(self.app_js, "providerDraftMarkup")
        self.assertIn('data-provider-draft-template', draft)
        self.assertIn('data-provider-draft-base-url', draft)
        self.assertIn('data-provider-draft-api-key-env', draft)
        self.assertIn('Secrets stay in your environment', draft)
        self.assertIn('api("/api/providers"', _function_body(self.app_js, "submitProviderDraft"))
        self.assertIn('api("/api/providers/remove"', _function_body(self.app_js, "removeProvider"))
        self.assertIn('window.confirm', _function_body(self.app_js, "removeProvider"))
        self.assertIn("info.description", connections)
        self.assertIn("info.auth_mode", connections)
        opener = _function_body(self.app_js, "openProviderWindow")
        self.assertIn("model: model || null", opener)
        self.assertIn('fragment.get("model")', self.app_js)
        init = _function_body(self.app_js, "init")
        self.assertIn("createChat(fragmentModel)", init)
        login = _function_body(self.app_js, "requestProviderLogin")
        self.assertIn("launchProviderLogin(provider)", login)
        self.assertNotIn("terminal", login.lower())
        self.assertNotIn('id="providerLoginDialog"', self.index_html)
        self.assertNotIn("Open sign-in again", connections)
        self.assertIn("Sign-in browser opened", connections)
        self.assertIn("data-provider-auth-code", connections)
        self.assertIn("only if Anthropic shows one", connections)
        confirmation = _function_body(self.app_js, "submitProviderAuthCode")
        self.assertIn("/code`,", confirmation)
        self.assertIn("JSON.stringify({ code })", confirmation)
        watcher = _function_body(self.app_js, "watchProviderLogin")
        self.assertIn("refreshBudgets(false)", watcher)
        self.assertIn("is ready", watcher)
        self.assertIn("default browser", _function_body(self.app_js, "launchProviderLogin"))

    def test_model_pickers_show_exact_ids_and_remember_last_choice(self):
        self.assertIn("function modelOptionLabel", self.app_js)
        self.assertIn("`${label} · ${value}`", self.app_js)
        self.assertNotIn("Default ·", self.app_js)
        self.assertIn('api("/api/preferences/provider"', self.app_js)
        self.assertIn("preferences?.work_models", self.app_js)
        self.assertIn("modelOptionLabel", self.chat_js)
        self.assertIn("state.model_catalog", self.chat_js)

    def test_provider_management_is_inline_and_model_catalog_is_removed(self):
        """Provider creation is inline; the old standalone model catalog is gone."""
        dialog = self.index_html.split('<dialog id="providerDialog">', 1)[1].split('</dialog>', 1)[0]
        self.assertIn('id="addProvider"', dialog)
        self.assertNotIn('id="modelManagement"', dialog)
        self.assertNotIn('id="modelCatalogPanel"', dialog)
        self.assertNotIn('Add or remove models', self.app_js)
        self.assertNotIn('renderModelCatalog', self.app_js)
        self.assertNotIn('addDashboardModel', self.app_js)
        self.assertNotIn('removeDashboardModel', self.app_js)
        self.assertNotIn('data-model-add', self.app_js)
        self.assertNotIn('data-model-remove', self.app_js)
        connections = _function_body(self.app_js, "renderProviderConnections")
        self.assertIn('providerDraftMarkup()', connections)
        self.assertIn('data-provider-remove', connections)
        self.assertIn('$("#addProvider").addEventListener("click", beginProviderDraft)', self.app_js)

    def test_provider_cards_use_backend_supplied_metadata(self):
        """Provider additions must not require editing a frontend allow-list."""
        state_decl = self.app_js[self.app_js.index("const state = {"):self.app_js.index("};", self.app_js.index("const state = {"))]
        self.assertRegex(
            state_decl,
            r"(?:providerMetadata|provider_metadata|providers)\s*:",
            "frontend state needs a backend-supplied provider metadata collection",
        )
        connections = _function_body(self.app_js, "renderProviderConnections")
        self.assertNotRegex(
            connections,
            r"\[\s*[\"']codex[\"']\s*,\s*[\"']claude[\"']\s*,\s*[\"']qwen[\"']\s*\]",
        )
        self.assertIn("providerIds()", connections)
        provider_ids = _function_body(self.app_js, "providerIds")
        self.assertIn("state.providers", provider_ids)
        labels = _function_body(self.app_js, "providerLabel")
        self.assertNotRegex(
            labels,
            r"\b(?:qwen|codex|claude)\s*:",
            "provider labels must come from provider metadata, not a frontend map",
        )

    def test_provider_window_carries_project_and_surfaces_initial_chat_failure(self):
        opener = _function_body(self.app_js, "openProviderWindow")
        sender = _function_body(self.app_js, "sendMessage")
        self.assertRegex(opener, r'state\.draftCwd\s*\|\|\s*activeChat\(\)\?\.cwd\s*\|\|\s*state\.defaultCwd')
        self.assertIn("cwd:", opener)
        self.assertRegex(sender, r'if \(!activeChat\(\)\)[\s\S]*?try \{[\s\S]*?createChat\(\)')
        self.assertRegex(sender, r'createChat\(\)[\s\S]*?catch \(error\)[\s\S]*?toast\(error\.message\)')
        self.assertIn('fragment.get("cwd")', self.app_js)

    def test_chat_window_inherits_the_active_provider_and_model(self):
        """Chat must use the provider/model selected in its spawning work window."""
        opener = _function_body(self.app_js, "openChatWindow")
        self.assertIn("state.windowProvider", opener)
        self.assertRegex(opener, r"(?:model|selectedModel).*modelSelect|modelSelect.*(?:model|selectedModel)")
        self.assertIn("provider:", opener)
        self.assertIn("model:", opener)
        self.assertIn('api("/api/chat/window"', self.app_js)

    def test_chat_uses_the_selected_provider_model_catalog(self):
        """Chat's picker should follow the same provider catalog as work sessions."""
        self.assertIn("model_catalog", self.chat_js)
        self.assertIn("windowProvider", self.chat_js)
        self.assertIn("pollProviderModels", self.chat_js)
        self.assertRegex(self.chat_js, r"model_catalog\s*\[.*provider|model_catalog\?\.")
        self.assertNotIn("CHAT_MODEL_OPTIONS", self.chat_js)

    def test_provider_action_creates_a_new_provider_window_only(self):
        """A provider window is a fixed session, not an in-place provider switch."""
        self.assertRegex(
            self.index_html,
            r'id="providerWindows"[\s\S]*?<span>Providers</span>',
        )
        connections = _function_body(self.app_js, "renderProviderConnections")
        self.assertIn('data-provider-window', connections)
        self.assertNotIn('data-provider-current', connections)
        self.assertNotIn('>Use here<', self.index_html)
        self.assertNotIn('Use a provider here', self.index_html)
        self.assertRegex(
            connections,
            r'data-provider-window="\$\{provider\}"',
        )

    def test_provider_logout_copy_is_scoped_to_the_selected_provider(self):
        logout = _function_body(self.app_js, "requestProviderLogout")
        self.assertIn("every PilferedParrot window that uses it", logout)
        self.assertIn("Other providers stay signed in", logout)

    def test_provider_windows_are_scoped_to_one_provider_and_one_window(self):
        """Dashboard state must never render another window's sessions."""
        visible_chats = _function_body(self.app_js, "visibleChats")
        render = _function_body(self.app_js, "render")
        self.assertRegex(
            visible_chats,
            r'filter\([\s\S]*(?:window_id|windowId)[\s\S]*state\.(?:windowId|providerWindow)',
        )
        self.assertRegex(
            visible_chats,
            r'(?:requested_provider|provider)[\s\S]*state\.(?:windowProvider|provider)',
        )
        self.assertRegex(render, r'(?:windowProvider|providerWindow)')

    def test_new_provider_window_is_maximized_windowed_fullscreen(self):
        launcher = (ASSET_DIR.parents[1] / "bin" / "pilferedparrot-app-browser").read_text(
            encoding="utf-8",
        )
        opener = _function_body(self.app_js, "openProviderWindow")
        self.assertIn("--start-maximized", launcher)
        self.assertRegex(opener, r'/api/provider/window')

    def test_provider_budget_is_shared_but_context_is_window_local(self):
        render_providers = _function_body(self.app_js, "renderProviders")
        render = _function_body(self.app_js, "render")
        context = _function_body(self.app_js, "contextUsageForModel")
        # Provider cards read the provider-wide budget collection; context is
        # computed from the active chat/model in this window.
        self.assertRegex(render_providers, r'state\.budgets\[provider\]')
        self.assertIn("contextUsageForModel", self.app_js)
        self.assertNotRegex(context, r'localStorage|sessionStorage')

    def test_history_surfaces_are_boxed_scroll_regions(self):
        for selector in (r"\.technical-history\s+\.chat-list", r"\.chat-window-history"):
            rule = rf"{selector}\s*\{{[^}}]*"
            self.assertRegex(self.app_css, rule + r"(?:overflow-y:\s*auto|overflow:\s*auto)")
            self.assertRegex(self.app_css, rule + r"border(?:-\w+)?\s*:")
            self.assertRegex(self.app_css, rule + r"border-radius\s*:")

    def test_chat_composer_and_model_card_use_default_dark_panel(self):
        for selector in ("chat-composer", "chat-model-card"):
            rule = re.search(rf"(?m)^\.{selector}\s*\{{([^}}]*)\}}", self.app_css)
            self.assertIsNotNone(rule)
            self.assertRegex(rule.group(1), r"background\s*:\s*(?:var\(--panel\)|#111821|#131c26)")
            self.assertNotRegex(rule.group(1), r"rgba\(")

    def test_main_window_can_open_its_persistent_chrome_theme_gallery(self):
        self.assertRegex(
            self.index_html,
            r'id="chromeTheme"[^>]*>[\s\S]*?Change theme[\s\S]*?</button>',
        )
        self.assertNotIn("Appearance", self.index_html)
        self.assertNotIn("private Chrome window", self.index_html)
        self.assertNotIn("chromeThemeNote", self.index_html)
        self.assertNotIn('name="theme-color"', self.index_html)
        self.assertNotIn('id="chromeTheme"', self.chat_html)
        picker = _function_body(self.app_js, "openChromeThemeGallery")
        self.assertIn('api("/api/browser/theme"', picker)
        self.assertIn("Add to Chrome", picker)
        apply_theme = _function_body(self.app_js, "applyBrowserTheme")
        self.assertIn('body.classList.toggle("chrome-theme"', apply_theme)
        self.assertIn('$("#chromeThemeLabel").textContent = "Change theme"', apply_theme)
        self.assertNotIn('chromeThemeNote', apply_theme)
        self.assertNotIn("private Chrome window", apply_theme)
        self.assertIn('--chrome-theme-background-image', apply_theme)
        refresh_theme = _function_body(self.app_js, "refreshBrowserTheme")
        self.assertIn('api("/api/browser/theme")', refresh_theme)
        self.assertIn('window.addEventListener("focus"', self.app_js)
        self.assertRegex(self.app_css, r"body\.chrome-theme\s*\{")

    def test_model_pickers_poll_the_selected_provider_when_opened(self):
        poll = _function_body(self.app_js, "pollProviderModels")
        self.assertIn("/api/providers/", poll)
        self.assertIn("/models", poll)
        self.assertIn('state.model_catalog[provider]', poll)
        self.assertRegex(
            self.app_js,
            r'\$\("#modelSelect"\)\.addEventListener\("pointerdown"[\s\S]*?pollProviderModels',
        )
        self.assertRegex(
            self.app_js,
            r'\$\("#providerConnectionList"\)\.addEventListener\("pointerdown"[\s\S]*?pollProviderModels',
        )
        self.assertIn("--chrome-theme-background-image", self.app_css)

    def test_work_and_chat_completion_paths_use_desktop_notification_helper(self):
        """Both polling surfaces notify the OS when a pending response completes."""
        helper_names = ("notifyCompletion", "notifyFinished", "showDesktopNotification")
        self.assertTrue(
            any(name in self.app_js for name in helper_names),
            "work-session UI needs a shared desktop notification helper",
        )
        self.assertTrue(
            any(name in self.chat_js for name in helper_names),
            "Chat UI needs a shared desktop notification helper",
        )
        for source, function_name in ((self.app_js, "schedulePoll"), (self.chat_js, "schedulePoll")):
            body = _function_body(source, function_name)
            self.assertRegex(body, r"(?:wasRunning|previouslyRunning|wasPending)")
            self.assertRegex(body, r"(?:notifyCompletion|notifyFinished|showDesktopNotification)")
        self.assertIn(
            '$("#chromeTheme").addEventListener("click", openChromeThemeGallery)',
            self.app_js,
        )

    def test_chat_window_applies_the_selected_chrome_theme(self):
        apply_theme = _function_body(self.chat_js, "applyBrowserTheme")
        self.assertIn('classList.toggle("chrome-theme"', apply_theme)
        self.assertIn('--chrome-theme-background-image', apply_theme)
        self.assertIn('meta[name="theme-color"]', apply_theme)
        refresh_theme = _function_body(self.chat_js, "refreshBrowserTheme")
        self.assertIn('api("/api/browser/theme")', refresh_theme)
        init = _function_body(self.chat_js, "init")
        self.assertIn('api("/api/browser/theme")', init)
        self.assertRegex(
            self.app_css,
            r"body\.chrome-theme\s+\.chat-window\s*\{[^}]*--chrome-theme-background-image",
        )

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
            r"\.provider-card-head\s*>\s*span\s*\{[^}]*align-items:\s*baseline",
        )

    def test_narrow_technical_panes_reduce_header_density(self):
        self.assertRegex(
            self.app_css,
            r"(?m)^\.main\s*\{[^}]*(?:container\s*:\s*\w+\s*/\s*inline-size|container-type\s*:\s*inline-size)",
        )
        self.assertRegex(self.app_css, r"@container(?:\s+\w+)?\s*\(max-width:\s*920px\)")
        self.assertRegex(self.app_css, r"@container(?:\s+\w+)?\s*\(max-width:\s*700px\)")

    def test_work_and_sidebar_panes_have_named_inline_size_containers(self):
        for selector, name in (("main", "work"), ("sidebar", "sidebar")):
            rule = re.search(rf"(?m)^\.{selector}\s*\{{([^}}]*)\}}", self.app_css)
            self.assertIsNotNone(rule, f".{selector} rule is missing")
            declarations = rule.group(1)
            named = re.search(
                rf"container\s*:\s*{name}\s*/\s*inline-size|"
                rf"container-name\s*:\s*{name}[;}}][^}}]*container-type\s*:\s*inline-size|"
                rf"container-type\s*:\s*inline-size[^}}]*container-name\s*:\s*{name}",
                declarations,
            )
            self.assertIsNotNone(named, f".{selector} must be a named inline-size container")

    def test_workspace_actions_are_grouped_below_the_sidebar_brand(self):
        sidebar = self.index_html.split('<aside class="sidebar"', 1)[1].split("</aside>", 1)[0]
        brand_end = sidebar.index('</div>')
        actions_start = sidebar.index('<nav class="sidebar-actions"')
        provider_status_start = sidebar.index('<section class="provider-status"')
        self.assertLess(brand_end, actions_start)
        self.assertLess(actions_start, provider_status_start)
        for control in ('newWorkSession', 'providerWindows', 'openChat'):
            self.assertEqual(sidebar.count(f'id="{control}"'), 1)
        self.assertLess(sidebar.index('id="newWorkSession"'), sidebar.index('id="providerWindows"'))
        self.assertLess(sidebar.index('id="newWorkSession"'), sidebar.index('id="openChat"'))
        self.assertRegex(
            sidebar,
            r'id="newWorkSession"[\s\S]*?<span>New Session</span>[\s\S]*?'
            r'id="providerWindows"[\s\S]*?<span>Providers</span>[\s\S]*?'
            r'id="openChat"[\s\S]*?<span>Chat</span>',
        )
        main = self.index_html.split('<main class="main"', 1)[1].split("</main>", 1)[0]
        self.assertNotIn('class="top-actions"', main)
        # Browser regressions check label visibility and overflow at desktop
        # and narrow widths; exact grid ratios/heights are presentation choices.

    def test_narrow_sidebar_adapts_context_and_keeps_history_shrink_safe(self):
        queries = _container_query_bodies(self.app_css, "sidebar")
        self.assertGreaterEqual(len(queries), 1, "session-sidebar needs a responsive state")
        self.assertTrue(
            any(".context-pie-card" in body and ".context-pie" in body for body in queries),
            "narrow sidebar rules must adapt the context card and pie",
        )
        sidebar_rule = re.search(r"(?m)^\.sidebar\s*\{([^}]*)\}", self.app_css)
        self.assertRegex(sidebar_rule.group(1), r"min-width\s*:\s*0")
        for selector in ("chat-item-title", "chat-item-meta span:first-child"):
            rule = re.search(rf"\.{selector}\s*\{{([^}}]*)\}}", self.app_css)
            self.assertIsNotNone(rule, f".{selector} rule is missing")
            self.assertRegex(
                rule.group(1),
                r"min-width\s*:\s*0|overflow\s*:\s*hidden|overflow-wrap\s*:\s*anywhere|word-break\s*:",
            )

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

    def test_provider_choice_lives_in_provider_window_dialog(self):
        dialog = self.index_html.split('<dialog id="providerDialog">', 1)[1].split('</dialog>', 1)[0]
        self.assertIn('Choose a provider', dialog)
        self.assertIn('data-provider-window', self.app_js)
        self.assertNotIn('id="providerSelect"', self.index_html)
        self.assertIn('id="modelSelect"', self.index_html)
        sender = _function_body(self.app_js, "sendMessage")
        self.assertIn('const selectedProvider = state.windowProvider', sender)

    def test_launcher_opens_main_app_maximized(self):
        launcher = (ASSET_DIR.parents[1] / "bin" / "pilferedparrot-app-browser").read_text(encoding="utf-8")
        self.assertIn("--start-maximized", launcher)
        self.assertIn("profile_dir=$state_root/pilferedparrot/chrome-profile", launcher)

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

    def test_true_app_relaunch_opens_a_fresh_work_session_but_reload_restores(self):
        init = _function_body(self.app_js, "init")
        self.assertIn("const launchedFromApp = Boolean(fragmentCapability)", self.app_js)
        self.assertRegex(
            init,
            r"if \(launchedFromApp\)[\s\S]*createChat\(fragmentModel\)",
        )
        self.assertRegex(
            init,
            r"else \{[\s\S]*sessionStorage\.getItem\(ACTIVE_CHAT_SESSION_KEY\)",
        )

    def test_xdg_open_fallback_uses_document_lifecycle_for_window_close(self):
        launcher = (ASSET_DIR.parents[1] / "bin" / "pilferedparrot-app-browser").read_text(
            encoding="utf-8",
        )
        fallback = launcher.split("if command -v xdg-open", 1)[1]
        self.assertIn('exec xdg-open "$url"', fallback)
        self.assertNotIn("--window-closed", fallback)
        self.assertIn('window.addEventListener("pagehide"', self.app_js)
        self.assertIn('/api/window/close', self.app_js)

    def test_chat_assets_are_served_as_separate_documents(self):
        work_markdown = self.index_html.index('<script src="/markdown.js"></script>')
        chat_markdown = self.chat_html.index('<script src="/markdown.js"></script>')
        self.assertIn('<script src="/app.js"></script>', self.index_html)
        self.assertIn('<script src="/chat.js"></script>', self.chat_html)
        self.assertNotIn('<script src="/chat.js"></script>', self.index_html)
        self.assertLess(work_markdown, self.index_html.index('<script src="/app.js"></script>'))
        self.assertLess(chat_markdown, self.chat_html.index('<script src="/chat.js"></script>'))

    def test_work_and_chat_use_only_the_shared_safe_markdown_renderer(self):
        for source in (self.app_js, self.chat_js):
            self.assertIn("globalThis.PilferedParrotMarkdown", source)
            self.assertIn("renderMarkdown(", source)
            self.assertNotRegex(source, r"function\s+(?:inlineMarkdown|markdown)\s*\(")
        self.assertIn("Object.freeze({ render: render, escapeHtml: escapeHtml })", self.markdown_js)
        self.assertNotIn("innerHTML", self.markdown_js)
        self.assertIn('target=\\\"_blank\\\" rel=\\\"noopener noreferrer\\\"', self.markdown_js)
        self.assertIn("http:", self.markdown_js)
        self.assertIn("https:", self.markdown_js)
        self.assertIn("mailto:", self.markdown_js)
        self.assertIn(".table-scroll", self.app_css)
        self.assertIn("blockquote", self.app_css)

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
