"""Credential-free trusted kernel controller; no execution on the outer host."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from judge.container_devices import validate_selection  # noqa: E402
from judge.container_gpu_runtime import GPUContainerRuntime  # noqa: E402
from judge.container_kernel_flow import serve_kernel  # noqa: E402
from judge.container_kernel_protocol import FINISH_MARGIN, prepare_request, prepared  # noqa: E402
from judge.container_transport import ContainerListener  # noqa: E402


def main():
    if len(sys.argv) != 2:
        raise ValueError("Expected one fixed device selection")
    selection = validate_selection(json.loads(sys.argv[1]))
    manifest = json.loads(Path("/opt/praximesh/driver-manifest.json").read_text())
    listener = ContainerListener(on_tick=lambda: None)
    channel = runtime = None
    try:
        channel = listener.accept()
        request = channel.receive()
        if type(request) is not dict or set(request) != {"event", "protocol", "target"}:
            raise ValueError("Unexpected kernel preparation")
        if request != prepare_request(request["target"]):
            raise ValueError("Unexpected kernel preparation protocol")
        identity = prepared(selection, manifest, request["target"])
        runtime = GPUContainerRuntime(
            device_spec=selection,
            driver_manifest=manifest,
            target=request["target"],
            deadline=listener.status["deadline"] - FINISH_MARGIN,
            on_tick=channel.connected,
        )
        serve_kernel(channel, runtime, identity)
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            if channel is not None:
                channel.close()
            listener.close()


if __name__ == "__main__":
    main()
