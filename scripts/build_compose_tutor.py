"""Download and compile the pinned optional CPU tutor, checking every source digest."""

import shutil
import subprocess
import tarfile
from pathlib import Path

from scripts.fetch_verified import download

COMMIT = "620d2225cc436ab0b001e3200e590e3b286ef9ab"
MODEL_REVISION = "23749fefcc72300e3a2ad315e1317431b06b590a"
MODEL_SHA256 = "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"


def main():
    root = Path("/build")
    root.mkdir(exist_ok=True)
    archive = root / "llama.tar.gz"
    download(
        f"https://codeload.github.com/ggml-org/llama.cpp/tar.gz/{COMMIT}",
        archive,
        "1d4a987d9bd048f6e93107e838026438df70fbde237fc0c69c63d600512b4bd9",
        64 * 1024**2,
    )
    with tarfile.open(archive) as source:
        source.extractall(root, filter="data")
    source_root = root / f"llama.cpp-{COMMIT}"
    for path in source_root.rglob("*"):
        if path.is_file() and path.name.lower().startswith(("license", "copying")):
            destination = root / "licenses" / path.relative_to(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    subprocess.run(
        [
            "cmake",
            "-S",
            str(root / f"llama.cpp-{COMMIT}"),
            "-B",
            "/build/out",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DGGML_NATIVE=OFF",
            "-DGGML_CPU=ON",
            "-DGGML_CUDA=OFF",
            "-DGGML_VULKAN=OFF",
            "-DGGML_METAL=OFF",
            "-DGGML_BLAS=OFF",
            "-DLLAMA_BUILD_TESTS=OFF",
            "-DLLAMA_BUILD_EXAMPLES=OFF",
            "-DLLAMA_BUILD_APP=OFF",
            "-DLLAMA_BUILD_UI=OFF",
            "-DLLAMA_USE_PREBUILT_UI=OFF",
            "-DLLAMA_BUILD_MTMD=OFF",
            "-DLLAMA_BUILD_TOOLS=ON",
            "-DLLAMA_SUBPROCESS=OFF",
            "-DLLAMA_OPENSSL=OFF",
        ],
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", "/build/out", "--target", "llama-server", "--parallel", "2"],
        check=True,
    )
    download(
        "https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/resolve/"
        f"{MODEL_REVISION}/Qwen3-0.6B-Q8_0.gguf",
        root / "tutor.gguf",
        MODEL_SHA256,
        639446688,
    )
    download(
        f"https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/raw/{MODEL_REVISION}/LICENSE",
        root / "LICENSE.qwen",
        "5de36594c10839788a8c589443a8ef9d8b8d17c65a1b5807206ae037fc36c6bd",
        20000,
    )


if __name__ == "__main__":
    main()
