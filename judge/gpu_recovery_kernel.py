"""Fixed independent recovery kernel, shared by reviewed GPU runtimes."""

from judge.gpu_contract import output_matches, reference_values
from judge.gpu_probes import VECTOR_REFERENCE
from judge.sandbox import SandboxUnavailable


def verify_device_session(session):
    """Controller comparison for a fixed known-correct kernel in an open session."""
    inputs = {"a": [float(i % 13 - 6) for i in range(257)], "b": [0.25] * 257}
    case = {"inputs": inputs}
    expected = reference_values("vector-add-f32-v1", inputs)
    session.compile(VECTOR_REFERENCE, "vector-add-f32-v1")
    values, _elapsed = session.execute(case)
    if not output_matches(values, expected, {"kind": "float", "absolute": 1e-5, "relative": 1e-5}):
        raise SandboxUnavailable("GPU correctness recovery failed")


def recover_device(runtime):
    """A fresh guest/context must compile and execute a fixed known-correct kernel."""
    with runtime.kernel_session("device-recovery", mode="run") as session:
        verify_device_session(session)
        session.finish()
