"""Exercise response identity rendering without a browser or provider connection."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ASSETS = Path(__file__).parents[1] / "pilferedparrot" / "web_assets"
NODE = shutil.which("node")
RUNNER = r'''
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");
globalThis.globalThis = globalThis;
eval(source);
const message = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(globalThis.PilferedParrotIdentity.render(message));
'''


@unittest.skipUnless(NODE, "Node.js is required for frontend execution")
class FrontendIdentityTests(unittest.TestCase):
    def render(self, message):
        result = subprocess.run(
            [NODE, "-e", RUNNER, str(ASSETS / "identity.js")],
            input=json.dumps(message), text=True, capture_output=True, check=True,
        )
        return result.stdout

    def test_details_show_routing_destination_and_reported_model(self):
        rendered = self.render({
            "role": "assistant", "id": "m1", "model": "qwen-local",
            "response_identity": {
                "provider": "qwen", "requested_model": "qwen-local",
                "endpoint_origin": "http://127.0.0.1:11434",
                "endpoint_kind": "loopback", "reported_models": ["qwen-local"],
                "model_mismatch": False,
            },
        })
        self.assertIn("Response details", rendered)
        self.assertIn("Local machine (loopback)", rendered)
        self.assertIn("http://127.0.0.1:11434", rendered)
        self.assertIn("qwen-local", rendered)
        self.assertIn("cannot verify", rendered)

    def test_different_identifiers_are_neutral_even_with_saved_mismatch_flag(self):
        for reported in ("provider-alias", "/models/Qwen3-Coder-Next-UD-Q4_K_XL.gguf"):
            with self.subTest(reported=reported):
                rendered = self.render({
                    "role": "assistant", "model": "qwen3-coder-next",
                    "response_identity": {
                        "requested_model": "qwen3-coder-next", "endpoint_kind": "loopback",
                        "reported_models": [reported], "model_mismatch": True,
                    },
                })
                self.assertIn("<summary>Response details</summary>", rendered)
                self.assertIn("qwen3-coder-next", rendered)
                self.assertIn(reported, rendered)
                self.assertIn("aliases", rendered)
                self.assertIn("cannot verify", rendered)
                self.assertNotIn("Model ID differs", rendered)
                self.assertNotIn("reported model differs", rendered)
                self.assertNotIn("response-identity-warning", rendered)
                self.assertNotIn("response-identity-mismatch", rendered)

    def test_cli_and_missing_model_are_explicit(self):
        rendered = self.render({
            "role": "assistant", "response_identity": {
                "endpoint_kind": "cli", "reported_models": [],
            },
        })
        self.assertIn("Provider CLI", rendered)
        self.assertIn("Provider-selected model", rendered)
        self.assertIn("Not reported", rendered)
        self.assertIn("PPI does not capture a server-reported model from this CLI", rendered)

    def test_all_identity_strings_are_escaped(self):
        rendered = self.render({
            "role": "assistant", "response_identity": {
                "requested_model": '<script>alert("x")</script>',
                "endpoint_origin": '" onmouseover="alert(1)',
                "endpoint_kind": "unknown", "reported_models": ["<bad>&"],
            },
        })
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&lt;bad&gt;&amp;", rendered)

    def test_old_messages_without_evidence_are_omitted(self):
        self.assertEqual(self.render({"role": "assistant", "model": "old"}), "")

    def test_polling_restores_open_panel_and_summary_focus_by_message_id(self):
        script = r'''
const fs = require("fs");
eval(fs.readFileSync(process.argv[1], "utf8"));
const summary = {};
globalThis.document = {activeElement: summary};
const before = {dataset: {responseIdentity: "message-a"}, open: true,
  querySelector: () => summary};
const captured = PilferedParrotIdentity.captureState({querySelectorAll: () => [before]});
let focused = false;
const after = {dataset: {responseIdentity: "message-a"}, open: false,
  querySelector: () => ({focus: () => { focused = true; }})};
const unrelated = {dataset: {responseIdentity: "message-b"}, open: false};
PilferedParrotIdentity.restoreState({querySelectorAll: () => [after, unrelated]}, captured);
process.stdout.write(JSON.stringify({open: after.open, focused, unrelated: unrelated.open}));
'''
        result = subprocess.run(
            [NODE, "-e", script, str(ASSETS / "identity.js")],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout), {
            "open": True, "focused": True, "unrelated": False,
        })


if __name__ == "__main__":
    unittest.main()
