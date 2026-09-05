"""Execute keyboard focus behavior without a browser dependency."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from test_frontend_invariants import _function_body

ASSETS = Path(__file__).parents[1] / "pilferedparrot" / "web_assets"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for frontend execution")
class FrontendAccessibilityTests(unittest.TestCase):
    def test_sidebar_close_restores_focus_only_when_a_mobile_sidebar_was_open(self):
        for filename, function_name in [
            ("app.js", "setSidebarOpen"), ("chat.js", "setChatSidebarOpen"),
        ]:
            source = (ASSETS / filename).read_text()
            body = _function_body(source, function_name)
            sync_name = "syncSidebarAccessibility" if filename == "app.js" else "syncChatSidebarAccessibility"
            sync_body = _function_body(source, sync_name)
            body = f"function {sync_name}() {{{sync_body}}}\n{body}"
            script = r'''
const assert = require('assert');
const body = JSON.parse(process.argv[1]);
for (const mobile of [false, true]) {
  for (const wasOpen of [false, true]) {
    const focus = [];
    const nodes = {};
    let currentOpen = wasOpen;
    const $ = selector => nodes[selector] ||= {
      classList: {
        contains: () => currentOpen,
        toggle(_name, value) { currentOpen = value; },
      },
      setAttribute() {},
      focus() { focus.push(selector); },
    };
    const matchMedia = () => ({matches: mobile});
    const close = new Function('$', 'matchMedia', 'open', body);
    close($, matchMedia, false);
    assert.deepEqual(focus, mobile && wasOpen ? [process.argv[2]] : []);
    const main = nodes['.main'] || nodes['.chat-window-conversation'];
    assert.equal(main.inert, false);
  }
}
'''
            trigger = "#openSidebar" if filename == "app.js" else "#toggleChatSidebar"
            with self.subTest(window=filename):
                subprocess.run(
                    [NODE, "-e", script, json.dumps(body), trigger],
                    capture_output=True, text=True, check=True, timeout=10,
                )
