"""Validate an application image in an ephemeral nonroot, offline container."""

import argparse
import subprocess
import uuid

PROBE = """
import os, subprocess, time, urllib.request
os.environ['DJANGO_SETTINGS_MODULE'] = 'praximesh.settings'
import django
django.setup()
from django.core.management import call_command
assert os.getuid() == 65532
call_command('migrate', interactive=False, verbosity=0)
call_command('content_import')
process = subprocess.Popen(['gunicorn', 'praximesh.wsgi:application', '--bind', '127.0.0.1:8000', '--workers', '1'])
try:
    deadline = time.monotonic() + 15
    while True:
        try:
            response = urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=2)
            assert response.status == 200 and b'unavailable' in response.read()
            break
        except OSError:
            if process.poll() is not None or time.monotonic() > deadline: raise
            time.sleep(0.2)
    for path in ['/', '/practice/', '/static/app.css', '/static/app.js']:
        assert urllib.request.urlopen('http://127.0.0.1:8000' + path, timeout=2).status == 200
    print('PASS: nonroot, readonly root, offline, migration, content, Gunicorn and static HTTP; judge disabled')
finally:
    process.terminate()
    process.wait(timeout=5)
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="praximesh:phase-a-check")
    options = parser.parse_args()
    name = f"praximesh-web-check-{uuid.uuid4().hex[:12]}"
    command = [
        "docker",
        "run",
        "--pull=never",
        "--rm",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=64",
        "--memory=512m",
        "--tmpfs=/tmp:rw,size=64m,mode=1777",
        "--tmpfs=/data:rw,size=64m,uid=65532,gid=65532",
        "-e",
        "SQLITE_PATH=/data/smoke.sqlite3",
        options.image,
        "python",
        "-c",
        PROBE,
    ]
    try:
        subprocess.run(command, check=True, timeout=45)
    finally:
        # --rm handles normal completion; this also handles a killed Docker client.
        remaining = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=^{name}$"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        if remaining.stdout.strip():
            subprocess.run(["docker", "rm", "-f", name], check=True, timeout=10)


if __name__ == "__main__":
    main()
