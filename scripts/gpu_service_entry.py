"""Internal trusted launch gate; prevent a queued service from starting late.

The service's independent runtime timer has already started before this gate
execs its trusted controller. Learner source is never accepted by this entry.
"""

import math
import os
import sys
import time
from pathlib import Path


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) < 4 or arguments[0] != "--start-before" or arguments[2] != "--":
        return 70
    try:
        before = float(arguments[1])
    except ValueError:
        return 70
    command = arguments[3:]
    if not math.isfinite(before) or not Path(command[0]).is_absolute():
        return 70
    if time.monotonic() >= before:
        return 70
    # No shell interpolation and no subprocess that could outlive the service.
    os.execv(command[0], command)


if __name__ == "__main__":
    sys.exit(main())
