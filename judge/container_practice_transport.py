"""Bounded framed pipes for one local trusted-controller invocation.

No subprocess, reconnect, command selection or descriptor inheritance is provided
here. The launcher must verify and supervise the original control container.
"""

import os
import select
import stat

from judge.gpu_transport import Channel
from judge.sandbox import SandboxUnavailable


class PipeChannel(Channel):
    """Use the existing frame/budget checks with independently owned pipe ends."""

    def __init__(self, incoming, outgoing, *, deadline, on_tick):
        self.incoming = self.outgoing = None
        self.deadline, self.on_tick = deadline, on_tick
        self.next_tick = 0
        self.sent = self.received = 0
        try:
            for name, descriptor in (("incoming", incoming), ("outgoing", outgoing)):
                if type(descriptor) is not int or descriptor < 0:
                    raise ValueError("Explicit local pipe descriptors are required")
                mode = os.fstat(descriptor).st_mode
                if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
                    raise ValueError("Local controller communication requires pipes or sockets")
                owned = os.dup(descriptor)
                setattr(self, name, owned)
                os.set_inheritable(owned, False)
                os.set_blocking(owned, False)
        except BaseException:
            self.close()
            raise

    def _transfer(self, size=None, data=None):
        writing = data is not None
        count = len(data) if writing else size
        offset, output = 0, bytearray()
        while offset < count:
            self.check()
            readable, writable, _ = select.select(
                [] if writing else [self.incoming], [self.outgoing] if writing else [], [], 0.05
            )
            if not (readable or writable):
                continue
            try:
                if writing:
                    changed = os.write(self.outgoing, data[offset : offset + 65536])
                else:
                    chunk = os.read(self.incoming, min(65536, count - offset))
                    changed = len(chunk)
                    output.extend(chunk)
            except BlockingIOError:
                continue
            except OSError:
                raise SandboxUnavailable("Local controller pipe failed") from None
            if not changed:
                raise SandboxUnavailable("Local controller disconnected before completion")
            offset += changed
        return bytes(output)

    def connected(self):
        self.check()
        try:
            if not select.select([self.incoming], [], [], 0)[0]:
                return
            value = os.read(self.incoming, 1)
        except BlockingIOError:
            return
        except OSError:
            raise SandboxUnavailable("Local controller pipe failed") from None
        if value:
            raise SandboxUnavailable("Unexpected input during local execution")
        raise SandboxUnavailable("Local client disconnected; retain original state")

    def expect_eof(self):
        while True:
            self.check()
            if not select.select([self.incoming], [], [], 0.05)[0]:
                continue
            try:
                value = os.read(self.incoming, 1)
            except BlockingIOError:
                continue
            except OSError:
                raise SandboxUnavailable("Local controller pipe failed before EOF") from None
            if value:
                raise SandboxUnavailable("Unexpected trailing local controller output")
            return

    def close(self):
        for name in ("incoming", "outgoing"):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()
