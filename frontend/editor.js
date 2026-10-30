import { basicSetup } from "codemirror";
import { EditorState, Prec, Transaction } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { indentWithTab, isolateHistory } from "@codemirror/commands";
import { python } from "@codemirror/lang-python";
import { javascript, javascriptLanguage } from "@codemirror/lang-javascript";
import { cuda } from "./cuda-language.js";
import { completeFromList } from "@codemirror/autocomplete";
import {
  indentUnit,
  HighlightStyle,
  syntaxHighlighting,
} from "@codemirror/language";
import { tags } from "@lezer/highlight";
import {
  lintGutter,
  setDiagnostics,
  setDiagnosticsEffect,
  openLintPanel,
} from "@codemirror/lint";
import { openSearchPanel } from "@codemirror/search";

const form = document.querySelector("#editor-form");
const source = document.querySelector("#source");
const host = document.querySelector("#code-editor");

function enhance() {
  const language = host.dataset.language;
  if (!["python", "javascript", "cuda"].includes(language)) return;
  const analysis = document.querySelector("#editor-analysis");
  const tools = document.querySelector("#editor-tools");
  const position = document.querySelector("#editor-position");
  let revision = 0,
    serial = 0,
    worker,
    debounce,
    formatting = false,
    staleFormat = false;
  const pending = new Map();
  const messages = {
    checking: gettext("正在本地检查…"),
    formatting: gettext("正在本地格式化…"),
    unavailable: gettext(
      "本地检查工具暂不可用；代码已保留，可以继续编辑、保存和提交。",
    ),
    stale: gettext("格式化期间代码已修改，请重新格式化。"),
    invalid: gettext("请先修复语法错误，再格式化。"),
    formatted: gettext("已格式化，可撤销。"),
    unchanged: gettext("代码格式无需调整。"),
    limit: gettext("代码不能超过 20000 字符。"),
  };

  function action(selector) {
    const button = form.querySelector(selector);
    if (button && !button.disabled) form.requestSubmit(button);
    return true;
  }
  function cursor(view) {
    const at = view.state.selection.main.head;
    const line = view.state.doc.lineAt(at);
    position.textContent = interpolate(gettext("第 %s 行，第 %s 列"), [
      line.number,
      at - line.from + 1,
    ]);
  }
  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: source.value,
      extensions: [
        EditorView.cspNonce.of(host.dataset.nonce),
        EditorView.domEventObservers({
          keydown(event, editor) {
            // Observe even when a completion/search handler consumes Escape.
            // Both closing its panel and leaving with Tab should remain possible.
            if (event.key === "Escape") editor.setTabFocusMode(2000);
          },
        }),
        basicSetup,
        language === "python"
          ? python()
          : language === "cuda"
            ? cuda()
            : [
                javascript(),
                javascriptLanguage.data.of({
                  autocomplete: completeFromList([
                    "Math",
                    "JSON",
                    "Array",
                    "Object",
                    "Number",
                    "String",
                    "Boolean",
                    "Map",
                    "Set",
                    "Infinity",
                    "NaN",
                    "undefined",
                    "parseInt",
                    "parseFloat",
                    "isFinite",
                    "isNaN",
                  ]),
                }),
              ],
        indentUnit.of(language === "javascript" ? "  " : "    "),
        lintGutter(),
        EditorView.contentAttributes.of({
          "aria-label": gettext("源代码"),
          "aria-describedby": "editor-help editor-keyboard-help",
          spellcheck: "false",
          autocapitalize: "off",
          autocomplete: "off",
        }),
        EditorState.phrases.of({
          Find: gettext("查找"),
          Replace: gettext("替换"),
          next: gettext("下一个"),
          previous: gettext("上一个"),
          all: gettext("全部"),
          "match case": gettext("区分大小写"),
          regexp: gettext("正则表达式"),
          "by word": gettext("全词匹配"),
          replace: gettext("替换"),
          "replace all": gettext("全部替换"),
          close: gettext("关闭"),
          "No diagnostics": gettext("没有检查提示"),
          Diagnostics: gettext("检查提示"),
          "No completions": gettext("没有补全候选"),
          Completions: gettext("代码补全"),
          "Fold line": gettext("折叠代码"),
          "Unfold line": gettext("展开代码"),
          "Go to line": gettext("跳转到行"),
          go: gettext("跳转"),
          "Selection deleted": gettext("已删除选中内容"),
        }),
        Prec.highest(
          keymap.of([
            {
              key: "Mod-s",
              run: () => action('[data-action="draft"]'),
              preventDefault: true,
            },
            {
              key: "Mod-Enter",
              run: () => action('[name="mode"][value="run"]'),
              preventDefault: true,
            },
            {
              key: "Alt-Shift-f",
              run: () => {
                request("format");
                return true;
              },
            },
          ]),
        ),
        keymap.of([indentWithTab]),
        EditorState.transactionFilter.of((transaction) => {
          if (
            transaction.docChanged &&
            transaction.newDoc.length > source.maxLength
          ) {
            analysis.textContent = messages.limit;
            return [];
          }
          return transaction;
        }),
        EditorState.transactionExtender.of((transaction) =>
          transaction.docChanged
            ? { effects: setDiagnosticsEffect.of([]) }
            : null,
        ),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            staleFormat = false;
            revision += 1;
            source.value = update.state.doc.toString();
            source.dispatchEvent(new Event("input", { bubbles: true }));
            clearTimeout(debounce);
            debounce = setTimeout(() => request("check"), 600);
          }
          if (update.docChanged || update.selectionSet) cursor(update.view);
        }),
        EditorView.theme(
          {
            "&": {
              color: "#edf4e7",
              backgroundColor: "#162d27",
              fontSize: "14px",
            },
            ".cm-content": {
              caretColor: "#e5f7cb",
              padding: "16px 0",
              minHeight: "360px",
            },
            ".cm-scroller": {
              fontFamily: "var(--mono)",
              lineHeight: "1.85",
              overflow: "auto",
            },
            ".cm-gutters": {
              color: "#a2b9ae",
              backgroundColor: "#162d27",
              borderRight: "1px solid #395349",
            },
            ".cm-activeLine, .cm-activeLineGutter": {
              backgroundColor: "#ffffff0c",
            },
            ".cm-cursor": { borderLeftColor: "#edf4e7" },
            "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection":
              { backgroundColor: "#446b59 !important" },
            ".cm-matchingBracket": {
              backgroundColor: "#517c55",
              color: "#fff",
            },
            ".cm-panels, .cm-tooltip": {
              backgroundColor: "#f6f7f2",
              color: "#18362f",
            },
            ".cm-searchMatch": {
              backgroundColor: "#b68a3655",
              outline: "1px solid #e6bb6f",
            },
            ".cm-searchMatch-selected": { backgroundColor: "#b68a3699" },
          },
          { dark: true },
        ),
        syntaxHighlighting(
          HighlightStyle.define([
            { tag: tags.keyword, color: "#dcb7ff" },
            { tag: [tags.string, tags.special(tags.string)], color: "#c5e899" },
            { tag: [tags.number, tags.bool, tags.null], color: "#f7c58c" },
            { tag: tags.comment, color: "#9ab1a6", fontStyle: "italic" },
            {
              tag: [
                tags.function(tags.variableName),
                tags.definition(tags.variableName),
              ],
              color: "#87dce5",
            },
            { tag: [tags.operator, tags.punctuation], color: "#d4ded6" },
          ]),
        ),
      ],
    }),
  });

  function unavailable() {
    worker?.terminate();
    worker = undefined;
    for (const item of pending.values()) clearTimeout(item.timeout);
    pending.clear();
    formatting = false;
    analysis.textContent = messages.unavailable;
  }
  function offset(location) {
    const row = Math.max(
      1,
      Math.min(view.state.doc.lines, location?.row ?? location?.line ?? 1),
    );
    const line = view.state.doc.line(row);
    return Math.min(
      line.to,
      line.from + Math.max(0, (location?.column ?? 1) - 1),
    );
  }
  function receive({ data }) {
    const item = pending.get(data.id);
    if (!item) return;
    clearTimeout(item.timeout);
    pending.delete(data.id);
    if (item.operation === "format") formatting = false;
    if (data.revision !== revision) {
      if (item.operation === "format") {
        staleFormat = true;
        analysis.textContent = messages.stale;
      }
      return;
    }
    if (data.unavailable) {
      unavailable();
      return;
    }
    const diagnostics = (data.diagnostics ?? []).map((entry) => {
      const from = offset(entry.start);
      return {
        from,
        to: Math.max(from, offset(entry.end ?? entry.start)),
        severity: entry.severity,
        message:
          entry.code === "cuda-syntax"
            ? gettext(
                "代码结构可能有语法错误；完整 CUDA 检查以隔离编译结果为准。",
              )
            : entry.message,
      };
    });
    view.dispatch(setDiagnostics(view.state, diagnostics));
    if (item.operation === "format") {
      if (data.invalid) {
        analysis.textContent = messages.invalid;
        openLintPanel(view);
        return;
      }
      if (data.formatted.length > source.maxLength) {
        analysis.textContent = messages.limit;
        return;
      }
      if (data.formatted === source.value) {
        analysis.textContent = messages.unchanged;
        return;
      }
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: data.formatted },
        annotations: [
          Transaction.userEvent.of("input.format"),
          isolateHistory.of("full"),
        ],
      });
      analysis.textContent = messages.formatted;
    } else {
      if (!staleFormat || item.show)
        analysis.textContent = diagnostics.length
          ? interpolate(gettext("发现 %s 条检查提示；不影响保存或提交。"), [
              diagnostics.length,
            ])
          : language === "cuda"
            ? gettext(
                "未发现代码结构错误；CUDA 类型、头文件和设备能力仍需隔离编译检查。",
              )
            : gettext("本地检查未发现问题；运行结果仍以评测为准。");
      if (item.show) openLintPanel(view);
    }
  }
  function request(operation, show = false) {
    if (operation === "format" && formatting) return;
    if (
      operation === "check" &&
      [...pending.values()].some((item) => item.revision === revision)
    )
      return;
    if (operation === "format" || show) staleFormat = false;
    try {
      if (!worker) {
        worker = new Worker(host.dataset.worker, { type: "module" });
        worker.onmessage = receive;
        worker.onerror = (event) => {
          event.preventDefault();
          unavailable();
        };
      }
      const id = ++serial;
      pending.set(id, {
        operation,
        revision,
        show,
        timeout: setTimeout(unavailable, 20000),
      });
      if (operation === "format") formatting = true;
      if (!staleFormat)
        analysis.textContent =
          operation === "format" ? messages.formatting : messages.checking;
      worker.postMessage({
        id,
        revision,
        operation,
        language,
        source: source.value,
        wasmURL: host.dataset.wasm,
        clangWasmURL: host.dataset.clangWasm,
      });
    } catch {
      unavailable();
    }
  }
  tools
    .querySelector('[data-command="format"]')
    .addEventListener("click", () => request("format"));
  tools
    .querySelector('[data-command="check"]')
    .addEventListener("click", () => request("check", true));
  tools
    .querySelector('[data-command="search"]')
    .addEventListener("click", () => openSearchPanel(view));
  window.addEventListener("pagehide", () => {
    clearTimeout(debounce);
    for (const item of pending.values()) clearTimeout(item.timeout);
    worker?.terminate();
  });
  source.hidden = true;
  host.hidden = false;
  tools.hidden = false;
  document.querySelector("#editor-keyboard-help").hidden = false;
  document.querySelector("#editor-fallback-help").hidden = true;
  cursor(view);
  request("check");
}

if (form && source && host) {
  try {
    enhance();
  } catch {
    source.hidden = false;
    host.hidden = true;
    // The original form remains usable if editor initialization fails.
  }
}
