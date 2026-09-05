"""Update notices stay isolated from session loading and window permissions."""
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from pilferedparrot import web_server
from test_web_server import FakeApp, bare_handler


class UpdateRouteTests(unittest.TestCase):
    def test_notice_script_is_served(self):
        handler = bare_handler(web_server.make_handler(FakeApp()), path="/provider-updates.js")
        handler._asset = MagicMock()
        handler.do_GET()
        handler._asset.assert_called_once_with("provider-updates.js", "text/javascript; charset=utf-8")

    def test_selected_provider_only_and_authorization_required(self):
        for context, allowed in [
            (None, False),
            ({"scope": "chat", "provider": "claude"}, False),
            ({"scope": "dashboard", "provider": "claude"}, False),
            ({"scope": "chat", "provider": "codex"}, True),
            ({"scope": "dashboard", "provider": "codex"}, True),
        ]:
            with self.subTest(context=context):
                app = FakeApp()
                app.provider_update = MagicMock(return_value={"status": "current"})
                handler = bare_handler(web_server.make_handler(app), path="/api/providers/codex/update")
                handler._request_capability_context = lambda: context
                handler._json = MagicMock()
                handler.do_GET()
                self.assertEqual(app.provider_update.called, allowed)
                if not allowed:
                    self.assertEqual(handler._json.call_args.args[1], 403)


@unittest.skipUnless(shutil.which("node"), "requires Node")
class UpdateNoticeTests(unittest.TestCase):
    def test_check_deduplication_stale_result_failure_and_reopening(self):
        source = Path(__file__).parents[1] / "pilferedparrot/web_assets/provider-updates.js"
        script = r'''
const assert = require('assert');
const fs = require('fs');
const node = {dataset: {}, append(value) { this.textContent += value; }};
globalThis.document = {querySelector: () => node, createTextNode: (s) => s};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const check = globalThis.PilferedParrotUpdates.check;
const pending = [];
const api = (path) => new Promise((resolve, reject) => pending.push({path, resolve, reject}));
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  check(api, 'codex', 'one', 'Codex');
  assert.equal(node.dataset.status, 'checking');
  check(api, 'codex', 'one', 'Codex');
  await tick();
  assert.equal(pending.length, 1);
  check(api, 'gemini', 'two', 'Gemini');
  await tick();
  pending[0].resolve({status: 'current', message: 'stale'});
  await tick();
  assert.notEqual(node.textContent, 'stale');
  pending[1].reject(new Error('offline'));
  await tick();
  assert.equal(node.dataset.status, 'unavailable');
  check(api, 'codex', 'one', 'Codex');
  await tick();
  assert.equal(pending.length, 3);
  pending[2].resolve({status: 'update_available', message: 'New release', update_command: 'update command'});
  await tick();
  assert.equal(node.textContent, 'New release Update: update command');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        subprocess.run([shutil.which("node"), "-e", script, str(source)], check=True,
                       capture_output=True, text=True, timeout=10)
