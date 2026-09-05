"""Execute reasoning picker behavior without a browser or provider connection."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ASSETS = Path(__file__).parents[1] / 'pilferedparrot' / 'web_assets'
NODE = shutil.which('node')
RUNNER = r'''
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
function definition(name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`Missing ${name}`);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}
const nodes = {};
const $ = id => nodes[id] ||= {};
const escapeHtml = s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const state = {model_catalog: {codex: {
  reasoning_default_label: 'Codex default', chat_reasoning_default_label: 'Chat default · Low',
  options: [
    {value: 'big', reasoning_efforts: ['low','high','ultra']},
    {value: 'small', reasoning_efforts: ['low','high']},
    {value: 'plain', reasoning_efforts: []},
  ]
}}};
const REASONING_LABELS = {low:'Low', high:'High', ultra:'Ultra'};
eval(definition('reasoningOptions'));
eval(definition('renderReasoningSelect'));
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
renderReasoningSelect(request.provider, request.model, request.effort, request.disabled, request.chat);
const select = $(request.chat ? '#chatReasoningSelect' : '#reasoningSelect');
process.stdout.write(JSON.stringify({select, hidden: $('#reasoningControl').hidden}));
'''

@unittest.skipUnless(NODE, 'Node.js is required for frontend execution')
class FrontendReasoningTests(unittest.TestCase):
    def render_picker(self, source, **request):
        result = subprocess.run(
            [NODE, '-e', RUNNER, str(ASSETS / source)],
            input=json.dumps(request), text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout)

    def test_both_surfaces_preserve_explicit_choice_and_distinct_defaults(self):
        for source, chat in [('app.js', False), ('chat.js', True)]:
            with self.subTest(source=source):
                result = self.render_picker(source, provider='codex', model='big', effort='ultra', chat=chat, disabled=True)
                self.assertEqual(result['select']['value'], 'ultra')
                self.assertTrue(result['select']['disabled'])
                self.assertFalse(result['hidden'])
                self.assertIn('Chat default · Low' if chat else 'Codex default', result['select']['innerHTML'])
                self.assertIn('delegate', result['select']['title'])
                default = self.render_picker(source, provider='codex', model='big', effort=None, chat=chat)
                self.assertEqual(default['select']['value'], '')

    def test_model_changes_reset_incompatible_choices_and_hide_unsupported_controls(self):
        for source in ['app.js', 'chat.js']:
            with self.subTest(source=source):
                result = self.render_picker(source, provider='codex', model='small', effort='ultra')
                self.assertEqual(result['select']['value'], '')
                self.assertNotIn('value="ultra"', result['select']['innerHTML'])
                for provider, model in [('claude', 'big'), ('codex', 'plain')]:
                    hidden = self.render_picker(source, provider=provider, model=model, effort='high')
                    self.assertTrue(hidden['hidden'])
                    self.assertEqual(hidden['select']['value'], '')

    def test_unknown_codex_model_uses_conservative_fallback(self):
        for source in ['app.js', 'chat.js']:
            result = self.render_picker(source, provider='codex', model='manual', effort='high')
            self.assertEqual(result['select']['value'], 'high')
            self.assertNotIn('value="ultra"', result['select']['innerHTML'])

if __name__ == '__main__':
    unittest.main()
