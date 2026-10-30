import { build } from "esbuild";
import {
  mkdir,
  readFile,
  writeFile,
  copyFile,
  readdir,
} from "node:fs/promises";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const output = path.join(root, "static/editor");
await mkdir(output, { recursive: true });
const result = await build({
  absWorkingDir: root,
  entryPoints: {
    editor: "frontend/editor.js",
    worker: "frontend/editor-worker.js",
  },
  outdir: output,
  bundle: true,
  format: "esm",
  platform: "browser",
  target: ["es2022"],
  minify: true,
  legalComments: "none",
  metafile: true,
});
if (
  Object.keys(result.metafile.inputs).filter((name) =>
    name.endsWith("@codemirror/state/dist/index.js"),
  ).length !== 1
) {
  throw new Error("CodeMirror must bundle exactly one shared state module.");
}
await copyFile(
  path.join(root, "node_modules/@astral-sh/ruff-wasm-web/ruff_wasm_bg.wasm"),
  path.join(output, "ruff.wasm"),
);

// Preserve licenses for every bundled package, including Rust notices in Ruff's license.
const packages = [
  ...new Set(
    Object.keys(result.metafile.inputs)
      .filter((file) => file.startsWith("node_modules/"))
      .map((file) =>
        file
          .split("/")
          .slice(0, file.split("/")[1].startsWith("@") ? 3 : 2)
          .join("/"),
      ),
  ),
].sort();
let notices =
  "Bundled editor dependencies. Rebuild with npm ci --ignore-scripts && npm run build:editor.\n";
for (const directory of packages) {
  const info = JSON.parse(
    await readFile(path.join(root, directory, "package.json")),
  );
  const license = (await readdir(path.join(root, directory))).find((name) =>
    /^licen[cs]e(\.md|\.txt)?$/i.test(name),
  );
  if (!license) throw new Error(`Missing license: ${directory}`);
  notices +=
    `\n\n===== ${info.name} ${info.version} =====\n\n` +
    (await readFile(path.join(root, directory, license), "utf8"));
}
await writeFile(path.join(output, "THIRD_PARTY_LICENSES.txt"), notices);
const digest = (data) => createHash("sha256").update(data).digest("hex");
const manifest = { inputs: {}, outputs: {} };
for (const name of [
  "package.json",
  "package-lock.json",
  "frontend/editor.js",
  "frontend/editor-worker.js",
  "scripts/build_editor.mjs",
]) {
  manifest.inputs[name] = digest(await readFile(path.join(root, name)));
}
for (const name of [
  "editor.js",
  "worker.js",
  "ruff.wasm",
  "THIRD_PARTY_LICENSES.txt",
]) {
  const data = await readFile(path.join(output, name));
  manifest.outputs[name] = { sha256: digest(data), bytes: data.length };
}
await writeFile(
  path.join(output, "manifest.json"),
  JSON.stringify(manifest, null, 2) + "\n",
);
console.log("Built local editor assets and dependency notices.");
