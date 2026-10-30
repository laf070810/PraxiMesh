// Exercise the shipped worker and real parsers/formatter. Sources are data;
// nothing is compiled for CUDA or executed as learner code.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const replies = [];
globalThis.self = { postMessage: (message) => replies.push(message) };
await import("../static/editor/worker.js");
const clangWasmURL = await readFile(
  new URL("../static/editor/clang-format.wasm", import.meta.url),
);

async function analyze(source, operation = "check") {
  replies.length = 0;
  await self.onmessage({
    data: {
      id: 1,
      revision: 3,
      language: "cuda",
      operation,
      source,
      clangWasmURL,
    },
  });
  assert.equal(replies.length, 1);
  assert.equal(replies[0].revision, 3);
  assert.equal(replies[0].unavailable, undefined);
  return replies[0];
}

test("CUDA qualifiers, pointer modifiers, attributes, raw strings and comments parse unchanged", async () => {
  const sources = [
    'extern "C" __global__ void solve(const float* a, const float* b, float* c, int n) { int i = blockIdx.x * blockDim.x + threadIdx.x; if (i < n) c[i] = a[i] + b[i]; }',
    "__device__ __forceinline__ float helper(float x) { return x; }",
    "__global__ __launch_bounds__(256, 2) void solve(float* __restrict__ a) { __shared__ __align__(16) float b[256]; __syncthreads(); }",
    "__device__ __constant__ float a; __host__ __device__ int f() { return 1; }",
    'const char* s = R"tag(__global__ { invalid text " //)tag"; // __device__ void invalid(\n',
    '/* __global__ { invalid */\n__global__ void solve() { const char* s = "__shared__ {"; }',
    "#define TILE 16\n__global__ void solve() { for (int i = 0; i < TILE; i++) {} }",
  ];
  for (const source of sources) {
    const reply = await analyze(source);
    assert.deepEqual(reply.diagnostics, [], source);
    assert.equal(reply.formatted, undefined);
  }
});

test("structure diagnostics retain UTF-16 line/column positions without claiming compilation", async () => {
  const reply = await analyze(
    '// 中文 😀\nextern "C" __global__ void solve() {\n    float x = ;\n}\n',
  );
  assert.ok(reply.diagnostics.length > 0);
  assert.ok(
    reply.diagnostics.every(
      (item) => item.code === "cuda-syntax" && item.severity === "warning",
    ),
  );
  assert.equal(reply.diagnostics[0].start.row, 3);
  assert.ok(reply.diagnostics[0].start.column >= 14);
});

test("clang-format changes layout, preserves CUDA tokens, and is idempotent", async () => {
  const source =
    'extern "C" __global__ void solve(float* out){int i=threadIdx.x;out[i]=i;}';
  const { formatted, diagnostics } = await analyze(source, "format");
  assert.deepEqual(diagnostics, []);
  assert.equal(
    formatted,
    'extern "C" __global__ void solve(float *out) {\n    int i = threadIdx.x;\n    out[i] = i;\n}',
  );
  assert.equal((await analyze(formatted, "format")).formatted, formatted);
});

test("unknown macros remain advisory and never stop formatting or editing", async () => {
  const result = await analyze(
    'extern "C" __custom_cuda_attribute__ void solve(){int i=1;}',
    "format",
  );
  assert.ok(result.diagnostics.length);
  assert.equal(result.invalid, undefined);
  assert.ok(result.formatted.includes("__custom_cuda_attribute__"));
});
