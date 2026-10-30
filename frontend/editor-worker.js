import initRuff, {
  PositionEncoding,
  Workspace,
} from "@astral-sh/ruff-wasm-web";
import { format } from "prettier/standalone";
import babel from "prettier/plugins/babel";
import estree from "prettier/plugins/estree";
import initClang, { format as formatClang } from "@wasm-fmt/clang-format/web";
import { parser as cudaParser } from "praximesh:cuda";

// Parse only. Never evaluate source, resolve imports, or load user configuration.
let workspace;
let clang;
function python(wasmURL) {
  workspace ??= initRuff({ module_or_path: wasmURL }).then(
    () =>
      new Workspace(
        {
          "target-version": "py312",
          "line-length": 88,
          lint: { select: ["E4", "E7", "E9", "F"] },
        },
        PositionEncoding.Utf16,
      ),
  );
  return workspace;
}

self.onmessage = async ({ data }) => {
  const { id, revision, operation, language, source, wasmURL, clangWasmURL } =
    data;
  if (typeof source !== "string" || source.length > 20000) return;
  try {
    let formatted, diagnostics;
    if (language === "python") {
      let ruff;
      try {
        ruff = await python(wasmURL);
      } catch {
        workspace = undefined;
        self.postMessage({ id, revision, unavailable: true });
        return;
      }
      diagnostics = ruff.check(source).map((item) => ({
        start: item.start_location,
        end: item.end_location,
        severity:
          !item.code ||
          item.code === "invalid-syntax" ||
          item.code.startsWith("E9")
            ? "error"
            : "warning",
        message: item.code ? `${item.code}: ${item.message}` : item.message,
      }));
      if (operation === "format") {
        if (diagnostics.some((item) => item.severity === "error")) {
          self.postMessage({ id, revision, invalid: true, diagnostics });
          return;
        }
        formatted = ruff.format(source);
      }
    } else if (language === "javascript") {
      // Explicit parsers only; no configuration discovery or runtime globals.
      formatted = await format(source, {
        parser: "babel",
        plugins: [babel, estree],
        printWidth: 88,
        tabWidth: 2,
      });
      diagnostics = [];
    } else if (language === "cuda") {
      diagnostics = [];
      const location = (offset) => {
        const lines = source.slice(0, offset).split("\n");
        return { row: lines.length, column: lines.at(-1).length + 1 };
      };
      cudaParser.parse(source).iterate({
        enter(node) {
          if (node.type.isError && diagnostics.length < 50) {
            diagnostics.push({
              start: location(node.from),
              end: location(node.to),
              severity: "warning",
              code: "cuda-syntax",
            });
          }
        },
      });
      if (operation === "format") {
        // Parser diagnostics are advisory. CUDA extensions/macros may exceed
        // this grammar; clang-format can still format them without compiling.
        try {
          clang ??= initClang(clangWasmURL);
          await clang;
        } catch {
          clang = undefined;
          self.postMessage({ id, revision, unavailable: true });
          return;
        }
        formatted = formatClang(
          source,
          "solution.cu",
          JSON.stringify({
            BasedOnStyle: "LLVM",
            IndentWidth: 4,
            ColumnLimit: 88,
          }),
        );
      }
    } else {
      self.postMessage({ id, revision, unavailable: true });
      return;
    }
    self.postMessage({
      id,
      revision,
      diagnostics,
      formatted: operation === "format" ? formatted : undefined,
    });
  } catch (error) {
    // Syntax errors are displayed as text, never injected as markup.
    self.postMessage({
      id,
      revision,
      invalid: true,
      diagnostics: [
        {
          start: error?.loc?.start ?? { row: 1, column: 1 },
          end: error?.loc?.end,
          severity: "error",
          message: String(error?.message ?? error).slice(0, 2000),
        },
      ],
    });
  }
};
