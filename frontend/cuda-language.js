import { cppLanguage } from "@codemirror/lang-cpp";
import { LRLanguage, indentNodeProp, foldNodeProp } from "@codemirror/language";
import {
  completeFromList,
  completeAnyWord,
  ifNotIn,
} from "@codemirror/autocomplete";
import { styleTags, tags } from "@lezer/highlight";
import { parser } from "praximesh:cuda";

const original = new Map(
  cppLanguage.parser.nodeSet.types.map((type) => [type.name, type]),
);
export const cudaLanguage = LRLanguage.define({
  name: "cuda",
  parser: parser.configure({
    props: [
      indentNodeProp.add((type) =>
        original.get(type.name)?.prop(indentNodeProp),
      ),
      foldNodeProp.add((type) => original.get(type.name)?.prop(foldNodeProp)),
      styleTags({ "CudaQualifier/... CudaAttribute/...": tags.modifier }),
    ],
  }),
  languageData: {
    commentTokens: { line: "//", block: { open: "/*", close: "*/" } },
    indentOnInput: /^\s*(?:case |default:|\{|\})$/,
    closeBrackets: {
      stringPrefixes: ["L", "u", "U", "u8", "LR", "UR", "uR", "u8R", "R"],
    },
    autocomplete: ifNotIn(
      [
        "String",
        "RawString",
        "CharLiteral",
        "LineComment",
        "BlockComment",
        "SystemLibString",
      ],
      completeFromList([
        ...[
          "__global__",
          "__device__",
          "__host__",
          "__shared__",
          "__constant__",
          "__managed__",
          "__restrict__",
          "__forceinline__",
          "__noinline__",
          "__launch_bounds__",
          "__align__",
          "__grid_constant__",
          "const",
          "constexpr",
          "extern",
          "if",
          "else",
          "for",
          "while",
          "return",
          "sizeof",
          "static",
        ],
        ...[
          "float",
          "double",
          "int",
          "unsigned",
          "void",
          "bool",
          "size_t",
          "dim3",
          "float2",
          "float4",
        ].map((label) => ({ label, type: "type" })),
        ...["threadIdx", "blockIdx", "blockDim", "gridDim", "warpSize"].map(
          (label) => ({ label, type: "variable" }),
        ),
        ...[
          "__syncthreads",
          "__syncwarp",
          "__shfl_sync",
          "__shfl_down_sync",
          "atomicAdd",
          "fmaf",
          "fabsf",
          "sqrtf",
        ].map((label) => ({ label, type: "function" })),
      ]),
    ),
  },
});

export function cuda() {
  return [
    cudaLanguage,
    cudaLanguage.data.of({
      autocomplete: ifNotIn(
        ["String", "RawString", "CharLiteral", "LineComment", "BlockComment"],
        completeAnyWord,
      ),
    }),
  ];
}
