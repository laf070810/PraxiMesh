"""Fixed CPU protocol fixture. Source is opaque data, never compiled or executed."""

import json
import os
import struct
import sys
import time
from pathlib import Path


def f32(value):
    return struct.unpack("f", struct.pack("f", value))[0]


def main():
    mode, label = sys.argv[1:]
    if (
        list(Path("/dev").glob("nvidia*"))
        or not os.statvfs("/").f_flag & os.ST_RDONLY
        or Path("/run/praximesh/status.json").exists()
    ):
        raise ValueError("CPU fixture containment differs")
    request = json.loads(sys.stdin.readline())
    if set(request) != {"protocol", "source", "operation"} or request["protocol"] != 1:
        raise ValueError("Protected task data reached the CPU guest")
    if request["operation"] != "vector-add-f32-v1":
        raise ValueError("CPU fixture supports only its fixed vector operation")
    if label == "task" and mode == "hang":
        time.sleep(60)
        return 70
    if label in {"task", "startup-check"} and mode == "syntax-error":
        return 3
    print(json.dumps({"event": "compiled", "architecture": [8, 6]}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        if set(request) not in ({"protocol", "inputs"}, {"protocol", "inputs", "repetitions"}):
            raise ValueError("Protected expected values reached the CPU guest")
        if label == "startup-check" and mode == "check-hang":
            time.sleep(60)
            return 70
        if label in {"task", "startup-check"} and mode == "runtime-error":
            return 2
        values = [
            f32(f32(a) + f32(b))
            for a, b in zip(request["inputs"]["a"], request["inputs"]["b"], strict=True)
        ]
        if (
            label in {"task", "startup-check"}
            and mode == "wrong-answer"
            or label == "device-recovery"
            and mode == "recovery-fail"
        ):
            values = [0.0] * len(values)
        print(json.dumps({"values": values}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
