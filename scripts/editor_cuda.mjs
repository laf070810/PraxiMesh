import { buildParserFile } from "@lezer/generator";
import { readFile } from "node:fs/promises";
import path from "node:path";

// Extend the locked upstream grammar, including its raw string/comment tokens.
// Never strip or rewrite a learner's source to make it parse. Keep generated
// terms and external tokenizers together; upstream term IDs are not compatible.
export async function cudaGrammar(root) {
  const directory = path.join(root, "node_modules/@lezer/cpp/src");
  let grammar = await readFile(path.join(directory, "cpp.grammar"), "utf8");
  for (const [anchor, addition] of [
    ["typeQualifier {", "CudaQualifier |"],
    ["AttributeSpecifier {", "CudaAttribute ArgumentList |"],
  ]) {
    if (grammar.split(anchor).length !== 2) {
      throw new Error(
        "Review the updated C++ grammar before building CUDA support.",
      );
    }
    grammar = grammar.replace(anchor, `${anchor}\n  ${addition}`);
  }
  grammar += `
CudaQualifier {
  kw<"__global__"> | kw<"__device__"> | kw<"__host__"> |
  kw<"__shared__"> | kw<"__constant__"> | kw<"__managed__"> |
  kw<"__restrict__"> | kw<"__grid_constant__"> |
  kw<"__forceinline__"> | kw<"__noinline__">
}
CudaAttribute { kw<"__launch_bounds__"> | kw<"__align__"> }
`;
  const generated = buildParserFile(grammar, { fileName: "cuda.grammar" });
  const modules = {
    parser: generated.parser,
    "parser.terms": generated.terms,
    tokens: await readFile(path.join(directory, "tokens.js"), "utf8"),
    highlight: await readFile(path.join(directory, "highlight.js"), "utf8"),
  };
  return {
    name: "cuda-grammar",
    setup(build) {
      build.onResolve({ filter: /^praximesh:cuda$/ }, () => ({
        path: "parser",
        namespace: "cuda",
      }));
      build.onResolve(
        { filter: /^\.\//, namespace: "cuda" },
        ({ path: name }) => ({
          path: name.slice(2).replace(/\.js$/, ""),
          namespace: "cuda",
        }),
      );
      build.onLoad({ filter: /.*/, namespace: "cuda" }, ({ path: name }) => {
        if (!(name in modules))
          throw new Error(`Unknown CUDA grammar module: ${name}`);
        return { contents: modules[name], loader: "js", resolveDir: root };
      });
    },
  };
}
