"""Exercise the actual new-session request and restart choice in JavaScript."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class FrontendSessionDefaultsTests(unittest.TestCase):
    def run_frontend(self, active, options, value):
        script = r'''
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
function definition(name, prefix = "function") {
  const start = source.indexOf(`${prefix} ${name}(`);
  return source.slice(start, source.indexOf("\n}", start) + 2);
}
let createChatPending = false, selectionSavePending = false;
const state = {windowProvider: "codex", chats: [], defaultCwd: "/tmp", initialized: true};
const nodes = {"#reasoningSelect": {value: input.value, options: Array(input.options)}};
const $ = key => nodes[key] ||= {focus() {}};
const activeChat = () => input.active ? {id: "old"} : null;
let request;
const api = async (path, init) => {
  request = JSON.parse(init.body);
  return {id: "new", cwd: "/tmp"};
};
const sessionStorage = {setItem() {}};
const ACTIVE_CHAT_SESSION_KEY = "active";
const resizePrompt = () => {}, render = () => {};
eval(definition("latestUsedChat"));
eval(definition("createChat", "async function"));
createChat("chosen-model").then(() => {
  const latest = latestUsedChat([
    {id: "background", updated_at: 999, last_used_order: 1},
    {id: "selected", updated_at: 2, last_used_order: 2},
  ]);
  process.stdout.write(JSON.stringify({request, latest: latest.id}));
});
'''
        result = subprocess.run(
            ["node", "-e", script, str(Path(__file__).parents[1] / "pilferedparrot/web_assets/app.js")],
            input=json.dumps(dict(active=active, options=options, value=value)),
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    def test_visible_model_and_reasoning_choices_are_explicit(self):
        for effort in ("high", ""):
            result = self.run_frontend(True, 3, effort)
            self.assertEqual(result["request"]["model"], "chosen-model")
            self.assertEqual(result["request"]["reasoning_effort"], effort or None)
            self.assertEqual(result["latest"], "selected")

    def test_launch_without_rendered_picker_leaves_inheritance_to_server(self):
        result = self.run_frontend(False, 0, "")
        self.assertNotIn("reasoning_effort", result["request"])
