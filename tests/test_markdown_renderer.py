"""Behavior checks for the dependency-free shared browser Markdown renderer."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ASSET = Path(__file__).parents[1] / "pilferedparrot" / "web_assets" / "markdown.js"
NODE = shutil.which("node")
NODE_RUNNER = r"""
global.window = globalThis;
const fs = require("fs");
eval(fs.readFileSync(process.argv[1], "utf8"));
const request = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(globalThis.PilferedParrotMarkdown.render(request.value, request.options));
"""


@unittest.skipUnless(NODE, "Node.js is needed to execute the browser renderer")
class MarkdownRendererTests(unittest.TestCase):
    def render(self, value, options=None):
        completed = subprocess.run(
            [NODE, "-e", NODE_RUNNER, str(ASSET)],
            input=json.dumps({"value": value, "options": options or {}}),
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout

    def test_renders_the_bounded_block_and_inline_subset(self):
        output = self.render(
            "# Heading\n"
            "A paragraph with a\nline break, *emphasis*, **strong**, and `code`.\n\n"
            "- alpha\n- beta\n\n"
            "1. first\n2. second\n\n"
            "> quoted text\n\n"
            "---\n\n"
            "| Left | Center | Right |\n"
            "| :--- | :---: | ---: |\n"
            "| a | b | c |\n\n"
            "```python\nprint('safe')\n```",
        )
        self.assertIn("<h1>Heading</h1>", output)
        self.assertIn("A paragraph with a<br>line break", output)
        self.assertIn("<em>emphasis</em>", output)
        self.assertIn("<strong>strong</strong>", output)
        self.assertIn("<code>code</code>", output)
        self.assertIn("<ul><li>alpha</li><li>beta</li></ul>", output)
        self.assertIn("<ol><li>first</li><li>second</li></ol>", output)
        self.assertIn("<blockquote><p>quoted text</p></blockquote>", output)
        self.assertIn("<hr>", output)
        self.assertIn('<div class="table-scroll"><table>', output)
        self.assertIn('class="markdown-align-center"', output)
        self.assertIn('class="markdown-align-right"', output)
        self.assertIn('data-language="python"', output)
        self.assertIn('class="language-python"', output)

    def test_escapes_html_and_renders_only_safe_absolute_links(self):
        output = self.render(
            '<script>globalThis.pwned = true</script> '
            '<img src="https://outside.invalid/pixel">\n'
            '[HTTP](https://example.test/path?q=one&x=two) '
            '[mail](mailto:person@example.test) '
            '[script](javascript:alert(1)) '
            '[data](data:text/html,boom) '
            '[relative](/private) '
            '[malformed](https://example.test/a(b))',
        )
        self.assertNotIn("<script>", output)
        self.assertNotIn("<img", output)
        self.assertIn("&lt;script&gt;globalThis.pwned = true&lt;/script&gt;", output)
        self.assertIn("&lt;img src=&quot;https://outside.invalid/pixel&quot;&gt;", output)
        self.assertEqual(output.count("<a "), 2)
        self.assertIn(
            '<a href="https://example.test/path?q=one&amp;x=two" '
            'target="_blank" rel="noopener noreferrer">HTTP</a>',
            output,
        )
        self.assertIn('<a href="mailto:person@example.test">mail</a>', output)
        for literal in (
            "[script](javascript:alert(1))", "[data](data:text/html,boom)",
            "[relative](/private)", "[malformed](https://example.test/a(b))",
        ):
            self.assertIn(literal, output)

    def test_preserves_intra_word_underscores_and_real_emphasis(self):
        output = self.render(
            "PARROT_UI_7281 _emphasis_ \\_literal\\_ `PARROT_UI_7281`",
        )
        self.assertIn("PARROT_UI_7281", output)
        self.assertIn("<em>emphasis</em>", output)
        self.assertIn(r"\_literal\_", output)
        self.assertIn("<code>PARROT_UI_7281</code>", output)
        self.assertNotIn("PARROT<em>UI</em>7281", output)

    def test_malformed_markup_and_unmatched_fence_keep_all_response_text(self):
        output = self.render(
            "Before **unclosed and [broken](https://)\n"
            "```sh\n<unsafe> & still visible",
            {"commandTarget": {"messageId": "assistant-1"}, "shellLanguages": ["sh"]},
        )
        self.assertIn("Before **unclosed", output)
        self.assertIn("[broken](https://)", output)
        self.assertIn("```sh", output)
        self.assertIn("&lt;unsafe&gt; &amp; still visible", output)
        self.assertNotIn("data-run-command", output)

    def test_inline_triple_backtick_text_remains_literal(self):
        output = self.render("inline ```bash echo malformed``` remains text")
        self.assertIn("inline ```bash echo malformed``` remains text", output)
        self.assertNotIn("<code>", output)

    def test_command_buttons_keep_raw_fence_indexes_and_single_line_rules(self):
        output = self.render(
            "```python\nprint('not shell')\n```\n"
            "```bash\necho safe\n```\n"
            "```sh\none\ntwo\n```\n"
            "```\nprintf plain\n```\n"
            "```bash\nunmatched",
            {
                "commandTarget": {"messageId": 'assistant-1"><img src=x>'},
                "shellLanguages": ["bash", "sh"],
            },
        )
        self.assertEqual(output.count("data-run-command"), 2)
        self.assertIn('data-block-index="1"', output)
        self.assertIn('data-block-index="3"', output)
        self.assertNotIn('data-block-index="0"', output)
        self.assertNotIn('data-block-index="2"', output)
        self.assertIn(
            'data-message-id="assistant-1&quot;&gt;&lt;img src=x&gt;"', output,
        )
        self.assertIn("```bash<br>unmatched", output)

    def test_only_complete_line_fences_advance_backend_compatible_index(self):
        output = self.render(
            "bad ```python\nx``` then\n```bash\necho ok\n```",
            {
                "commandTarget": {"messageId": "assistant-2"},
                "shellLanguages": ["bash"],
            },
        )
        self.assertEqual(output.count("data-run-command"), 1)
        self.assertIn('data-block-index="0"', output)
        self.assertIn("bad ```python", output)

    def test_nested_quote_fences_render_but_never_offer_terminal_actions(self):
        output = self.render(
            "> outer\n"
            "> > nested\n"
            "> > ```bash\n"
            "> > echo quoted\n"
            "> > ```\n\n"
            "```bash\necho top-level\n```",
            {
                "commandTarget": {"messageId": "assistant-quoted"},
                "shellLanguages": ["bash"],
            },
        )
        self.assertEqual(output.count("<blockquote>"), 2)
        self.assertIn("echo quoted", output)
        self.assertEqual(output.count("data-run-command"), 1)
        self.assertIn('data-block-index="0"', output)
        self.assertIn("echo top-level", output)

    def test_tables_require_a_delimiter_row_and_preserve_escaped_pipes(self):
        output = self.render(
            "This | is table-like prose\n"
            "but | has no delimiter row\n\n"
            "| Command | Meaning |\n"
            "| --- | --- |\n"
            r"| `printf a\|b` | a \| b |" "\n",
        )
        self.assertEqual(output.count("<table>"), 1)
        self.assertIn("This | is table-like prose<br>but | has no delimiter row", output)
        self.assertIn("<code>printf a|b</code>", output)
        self.assertIn("<td class=\"markdown-align-left\">a | b</td>", output)

    def test_long_input_and_deep_quotes_are_bounded_and_fully_escaped(self):
        long_text = "x" * 200_000 + "<script>bad()</script>"
        output = self.render(long_text)
        self.assertIn("x" * 1024, output)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", output)
        self.assertNotIn("<script>", output)

        deeply_quoted = "> " * 2_000 + "<img src=x>"
        quoted_output = self.render(
            deeply_quoted,
            {
                "commandTarget": {"messageId": "assistant-deep"},
                "shellLanguages": ["bash"],
            },
        )
        self.assertEqual(quoted_output.count("<blockquote>"), 16)
        self.assertIn("&lt;img src=x&gt;", quoted_output)
        self.assertNotIn("<img", quoted_output)
        self.assertNotIn("data-run-command", quoted_output)

    def test_oversized_single_line_shell_block_is_display_only(self):
        command = "x" * 4_001
        output = self.render(
            "```bash\n" + command + "\n```",
            {
                "commandTarget": {"messageId": "assistant-long"},
                "shellLanguages": ["bash"],
            },
        )
        self.assertIn(command, output)
        self.assertNotIn("data-run-command", output)


if __name__ == "__main__":
    unittest.main()
