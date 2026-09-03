(function (global) {
  "use strict";

  var MAX_QUOTE_DEPTH = 16;
  var MAX_RUNNABLE_COMMAND_LENGTH = 4000;

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (character) {
      return {
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
      }[character];
    });
  }

  function safeUrl(value) {
    var candidate = String(value || "").trim();
    if (!candidate || !/^(?:https?:\/\/|mailto:)/iu.test(candidate) || /[\u0000-\u0020()<>"']/u.test(candidate)) return null;
    try {
      var parsed = new URL(candidate);
      if ((parsed.protocol === "http:" || parsed.protocol === "https:") && !parsed.hostname) return null;
      if (parsed.protocol === "mailto:" && !parsed.pathname) return null;
      return parsed.href;
    } catch (_error) {
      return null;
    }
  }

  function inline(value) {
    var source = String(value || "");
    var output = "";
    var cursor = 0;
    var patterns = [
      /(?<!`)`([^`\n]+)`(?!`)/g,
      /!?\[([^\]\n]+)\]\(([^)\n]+)\)/g,
      /\*\*([^*\n]+)\*\*/g,
      /__([^_\n]+)__/g,
      /\*([^*\n]+)\*/g,
      /_([^_\n]+)_/g,
    ];
    while (cursor < source.length) {
      var match = null;
      var patternIndex = -1;
      patterns.forEach(function (pattern, index) {
        pattern.lastIndex = cursor;
        var candidate = pattern.exec(source);
        if (candidate && (!match || candidate.index < match.index)) {
          match = candidate;
          patternIndex = index;
        }
      });
      if (!match) {
        output += escapeHtml(source.slice(cursor));
        break;
      }
      output += escapeHtml(source.slice(cursor, match.index));
      if (patternIndex === 0) {
        output += "<code>" + escapeHtml(match[1]) + "</code>";
      } else if (patternIndex === 1) {
        var url = safeUrl(match[2]);
        var label = escapeHtml(match[1]);
        if (!url || match[0].charAt(0) === "!") {
          output += escapeHtml(match[0]);
        } else {
          var external = /^https?:$/u.test(new URL(url).protocol);
          output += "<a href=\"" + escapeHtml(url) + "\"" +
            (external ? " target=\"_blank\" rel=\"noopener noreferrer\"" : "") +
            ">" + label + "</a>";
        }
      } else {
        var tag = patternIndex < 4 ? "strong" : "em";
        output += "<" + tag + ">" + escapeHtml(match[1]) + "</" + tag + ">";
      }
      cursor = match.index + match[0].length;
    }
    return output;
  }

  // Only complete, line-delimited triple-backtick fences belong to the
  // supported subset. Inline and unmatched delimiters remain escaped text.
  function fenceLines(value) {
    var lines = String(value == null ? "" : value).split("\n");
    var result = [];
    var blockIndex = 0;
    var index = 0;
    while (index < lines.length) {
      var opening = lines[index].match(/^ {0,3}```(?:([A-Za-z0-9_+-]+))?[ \t]*$/u);
      if (!opening) {
        result.push({ type: "line", value: lines[index] });
        index += 1;
        continue;
      }
      var closing = index + 1;
      while (closing < lines.length && !/^ {0,3}```[ \t]*$/u.test(lines[closing])) closing += 1;
      if (closing >= lines.length) {
        // Preserve the opening marker and all following text when unmatched.
        result.push({ type: "line", value: lines[index] });
        index += 1;
        continue;
      }
      result.push({
        type: "fence",
        language: opening[1] ? opening[1].toLowerCase() : "",
        code: lines.slice(index + 1, closing).join("\n").trim(),
        blockIndex: blockIndex,
      });
      blockIndex += 1;
      index = closing + 1;
    }
    return result;
  }

  function isEscaped(text, position) {
    var slashes = 0;
    for (var index = position - 1; index >= 0 && text.charAt(index) === "\\"; index -= 1) slashes += 1;
    return slashes % 2 === 1;
  }

  function tableCells(row) {
    var text = row.trim();
    if (text.charAt(0) === "|") text = text.slice(1);
    if (text.charAt(text.length - 1) === "|" && !isEscaped(text, text.length - 1)) text = text.slice(0, -1);
    var cells = [];
    var cell = "";
    for (var index = 0; index < text.length; index += 1) {
      if (text.charAt(index) === "\\" && text.charAt(index + 1) === "|") {
        cell += "|";
        index += 1;
      } else if (text.charAt(index) === "|") {
        cells.push(cell.trim());
        cell = "";
      } else {
        cell += text.charAt(index);
      }
    }
    cells.push(cell.trim());
    return cells;
  }

  function tableBlock(blocks, start) {
    if (start + 1 >= blocks.length || blocks[start].type !== "line" ||
        blocks[start + 1].type !== "line" || blocks[start].value.indexOf("|") < 0) return null;
    var header = tableCells(blocks[start].value);
    var delimiters = tableCells(blocks[start + 1].value);
    if (header.length < 2 || header.length !== delimiters.length ||
        !delimiters.every(function (cell) { return /^:?-{3,}:?$/u.test(cell); })) return null;
    var alignment = delimiters.map(function (cell) {
      if (/^:-+:$/u.test(cell)) return "center";
      if (/^:-+/u.test(cell)) return "left";
      if (/-+:$/u.test(cell)) return "right";
      return "left";
    });
    var columns = header.length;
    var body = [];
    var index = start + 2;
    while (index < blocks.length && blocks[index].type === "line" && blocks[index].value.trim() && blocks[index].value.indexOf("|") >= 0) {
      var row = tableCells(blocks[index].value);
      if (row.length !== columns) break;
      body.push(row);
      index += 1;
    }
    return { end: index, header: header, body: body, columns: columns, alignment: alignment };
  }

  function renderBlocks(value, options, quoteDepth) {
    var blocks = fenceLines(value);
    var output = "";
    var index = 0;
    var shellLanguages = options.shellLanguages || [];
    function isShell(language) {
      return !language || (typeof shellLanguages.has === "function"
        ? shellLanguages.has(language) : Array.isArray(shellLanguages) && shellLanguages.indexOf(language) >= 0);
    }
    while (index < blocks.length) {
      var item = blocks[index];
      if (item.type === "fence") {
        var code = item.code;
        var language = item.language;
        var languageClass = language ? " language-" + escapeHtml(language) : "";
        var attrs = language ? " data-language=\"" + escapeHtml(language) + "\"" : "";
        var button = "";
        var target = options.commandTarget;
        if (target && isShell(language) && code && code.indexOf("\n") < 0 &&
            code.length <= MAX_RUNNABLE_COMMAND_LENGTH) {
          button = "<button type=\"button\" class=\"run-command\" data-run-command data-message-id=\"" +
            escapeHtml(target.messageId) + "\" data-block-index=\"" + item.blockIndex +
            "\" title=\"Run in terminal\" aria-label=\"Run command in terminal\">▶</button>";
        }
        output += "<div class=\"code-block" + (button ? " runnable" : "") + "\"" + attrs + ">" +
          button + "<pre><code class=\"" + languageClass.slice(1) + "\">" + escapeHtml(code) +
          "</code></pre></div>";
        index += 1;
        continue;
      }
      var line = item.value;
      if (!line.trim()) { index += 1; continue; }
      var heading = line.match(/^ {0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$/u);
      if (heading) {
        output += "<h" + heading[1].length + ">" + inline(heading[2]) + "</h" + heading[1].length + ">";
        index += 1; continue;
      }
      if (/^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/u.test(line)) {
        output += "<hr>"; index += 1; continue;
      }
      var table = tableBlock(blocks, index);
      if (table) {
        function alignedCell(tag, cell, column) {
          var alignment = table.alignment[column] || "left";
          return "<" + tag + " class=\"markdown-align-" + alignment + "\">" + inline(cell) + "</" + tag + ">";
        }
        output += "<div class=\"table-scroll\"><table><thead><tr>" + table.header.map(function (cell, column) { return alignedCell("th", cell, column); }).join("") + "</tr></thead><tbody>" +
          table.body.map(function (row) { return "<tr>" + row.slice(0, table.columns).map(function (cell, column) { return alignedCell("td", cell, column); }).join("") + "</tr>"; }).join("") + "</tbody></table></div>";
        index = table.end; continue;
      }
      var list = line.match(/^\s*([-+*])[ \t]+(.+)$/u) || line.match(/^\s*(\d+)[.)][ \t]+(.+)$/u);
      if (list) {
        var ordered = /^\s*\d/.test(line), tag = ordered ? "ol" : "ul";
        output += "<" + tag + ">";
        while (index < blocks.length && blocks[index].type === "line") {
          var entry = blocks[index].value.match(ordered ? /^\s*\d+[.)][ \t]+(.+)$/u : /^\s*[-+*][ \t]+(.+)$/u);
          if (!entry) break;
          output += "<li>" + inline(entry[1]) + "</li>"; index += 1;
        }
        output += "</" + tag + ">"; continue;
      }
      if (/^\s*>/.test(line)) {
        var quote = [];
        while (index < blocks.length && blocks[index].type === "line" && /^\s*>/.test(blocks[index].value)) {
          quote.push(blocks[index++].value.replace(/^\s*>[ \t]?/u, ""));
        }
        if (quoteDepth >= MAX_QUOTE_DEPTH - 1) {
          output += "<blockquote><p>" + quote.map(inline).join("<br>") + "</p></blockquote>";
        } else {
          // Quoted code is display-only. It must never acquire a terminal action.
          output += "<blockquote>" + renderBlocks(quote.join("\n"), {
            shellLanguages: shellLanguages,
          }, quoteDepth + 1) + "</blockquote>";
        }
        continue;
      }
      var paragraph = [line]; index += 1;
      while (index < blocks.length && blocks[index].type === "line" && blocks[index].value.trim()) {
        var next = blocks[index].value;
        if (/^ {0,3}(#{1,6})[ \t]+/.test(next) || /^\s*[-+*][ \t]+/.test(next) || /^\s*\d+[.)][ \t]+/.test(next) || /^\s*>/.test(next) ||
            /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/u.test(next) || tableBlock(blocks, index)) break;
        paragraph.push(next); index += 1;
      }
      output += "<p>" + paragraph.map(inline).join("<br>") + "</p>";
    }
    return output;
  }

  function render(value, options) {
    options = options || {};
    var source = String(value == null ? "" : value).replace(/\r/g, "");
    return renderBlocks(source, options, 0);
  }

  global.PilferedParrotMarkdown = Object.freeze({ render: render, escapeHtml: escapeHtml });
}(window));
