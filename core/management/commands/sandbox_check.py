import hashlib
import json
import os
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from judge.backends import configured_sandbox
from judge.sandbox import SandboxUnavailable

PROBES = [
    (
        "python-version",
        "import sys\ndef solve(): return list(sys.version_info[:3])",
        "ok",
        [3, 11, 15],
    ),
    ("correct", "def solve(): return 42", "ok", 42),
    ("syntax", "def solve(: pass", "syntax_error", None),
    ("exception", "def solve(): return 1/0", "runtime_error", None),
    ("timeout", "def solve():\n while True: pass", "time_limit", None),
    ("memory", "def solve():\n a = bytearray(512*1024*1024)\n return len(a)", "memory_limit", None),
    ("output", "def solve():\n print('x'*100000)\n return 1", "output_limit", None),
    ("nonroot", "import os\ndef solve(): return os.getuid() == 65534", "ok", True),
    (
        "no-secrets",
        "import os\ndef solve(): return not any(k in os.environ for k in ['APP_SECRET_KEY', 'DATABASE_URL', 'AWS_SECRET_ACCESS_KEY'])",
        "ok",
        True,
    ),
    (
        "read-only",
        "def solve():\n try:\n  open('/praximesh-write-test', 'w').write('x')\n  return False\n except OSError: return True",
        "ok",
        True,
    ),
    (
        "no-network",
        "import socket\ndef solve():\n s=socket.socket()\n s.settimeout(0.3)\n try:\n  s.connect(('1.1.1.1', 443))\n  return False\n except OSError: return True\n finally: s.close()",
        "ok",
        True,
    ),
    (
        "pid-limit",
        "import os, signal\ndef solve():\n children=[]\n try:\n  for i in range(40):\n   pid=os.fork()\n   if pid==0:\n    signal.pause()\n    os._exit(0)\n   children.append(pid)\n  return False\n except OSError: return len(children)<40\n finally:\n  for pid in children:\n   os.kill(pid, signal.SIGKILL)\n   os.waitpid(pid,0)",
        "ok",
        True,
    ),
    (
        "storage-limit",
        "def solve():\n try:\n  for i in range(16):\n   with open('/tmp/probe'+str(i), 'wb') as f: f.write(b'x'*1048576)\n  return False\n except OSError: return True",
        "ok",
        True,
    ),
    (
        "isolation-write",
        "def solve():\n open('/tmp/private-probe','w').write('not shared')\n return True",
        "ok",
        True,
    ),
    (
        "isolation-read",
        "import os\ndef solve(): return not os.path.exists('/tmp/private-probe') and not os.path.exists('/var/run/docker.sock')",
        "ok",
        True,
    ),
]


def probes_for(identity):
    if identity.get("policy") == "wasi-python-function-v1":
        from judge.wasi_probes import PROBES as wasi_probes

        return wasi_probes
    if identity.get("policy") != "gvisor-python-function-v1":
        raise SandboxUnavailable("Unknown isolation policy.")
    return PROBES


def suite_digest(identity):
    return hashlib.sha256(json.dumps(probes_for(identity)).encode()).hexdigest()


def check_receipt(identity):
    try:
        receipt = json.loads(Path(settings.JUDGE_RECEIPT).read_text())
    except (OSError, ValueError) as exc:
        raise SandboxUnavailable(
            "Run sandbox_check on this judge host before starting the worker."
        ) from exc
    if (
        receipt.get("identity") != identity
        or receipt.get("suite") != suite_digest(identity)
        or not 0 <= time.time() - receipt.get("created", 0) < 86400
        or receipt.get("passed") != [p[0] for p in probes_for(identity)]
    ):
        raise SandboxUnavailable(
            "Sandbox receipt expired or does not match current runtime/policy."
        )


class Command(BaseCommand):
    help = "Run real configured-backend isolation probes; write a 24-hour receipt only on success."

    def handle(self, *args, **options):
        path = Path(settings.JUDGE_RECEIPT)
        path.unlink(missing_ok=True)
        try:
            sandbox = configured_sandbox()
            identity = sandbox.preflight()
            passed = []
            for name, source, verdict, expected in probes_for(identity):
                result = sandbox.execute(source, "solve", [])
                if result.verdict != verdict or (
                    verdict == "ok"
                    and (type(result.value) is not type(expected) or result.value != expected)
                ):
                    raise SandboxUnavailable(f"Probe failed: {name}; result={result.verdict}")
                passed.append(name)
                self.stdout.write(f"PASS {name}")
            # Every probe must clean up its container. Do not remove unrelated containers.
            remaining = b""
            if identity["policy"] == "gvisor-python-function-v1":
                remaining = sandbox.command(["ps", "-aq", "--filter", "label=praximesh.judge=true"])
            if remaining.strip():
                raise SandboxUnavailable(
                    "Judge containers remain; run checks with workers stopped and investigate cleanup."
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(
                    {
                        "created": time.time(),
                        "identity": identity,
                        "suite": suite_digest(identity),
                        "passed": passed,
                    },
                    indent=2,
                )
            )
            os.chmod(temp, 0o600)
            temp.replace(path)
        except (SandboxUnavailable, ValueError, KeyError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            "Real isolation probes passed. This is a technical gate, not public-release authorization."
        )
