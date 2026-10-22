# WASI SDK 24 runtime notices

`LICENSES.txt` preserves the licensing documents from the exact wasi-libc and
LLVM compiler-rt source commits used by the pinned WASI SDK 24 release.
`sources.json` records their immutable upstream URLs and SHA-256 digests, plus
the combined notice digest. The SDK's binary archive itself did not include
these documents; the build recipe copies the combined file into the interpreter
bundle alongside CPython's own LICENSE.

This preserves upstream notices. It does not select a license for PraxiMesh or
replace the maintainer's review of redistribution obligations. Python packages
installed in the worker image retain their distribution metadata and licenses.
