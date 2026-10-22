"""Control one dedicated Caddy through its private Unix administration socket.

The caller must serialize changes and durably journal exposure intent BEFORE
activate(). A validated restart configuration is fsynced before the live reload;
if reload or its confirmation fails, the file records intent, not observed state.
Use status() to inspect the running proxy and recover from that ambiguity. This
module never starts Caddy, contacts a shared TCP admin endpoint, or installs trust.

Primary references: https://caddyserver.com/docs/api (ETag/If-Match),
https://caddyserver.com/docs/command-line#caddy-adapt and
https://caddyserver.com/docs/caddyfile/options#skip-install-trust .
"""

import errno
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import stat
import struct
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

MAX_CONFIG_BYTES = 1024 * 1024


class ProxyError(RuntimeError):
    """The dedicated proxy could not be safely inspected or changed."""


def _path(value, name):
    # Caddy expands {$ENV} before tokenizing, including within quoted strings.
    if not isinstance(value, (str, Path)) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(value)):
        raise ProxyError(f"{name} must be an absolute path without special characters")
    path = Path(value)
    if ".." in path.parts or str(path) != str(value):
        raise ProxyError(f"{name} must be a normalized absolute path")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ProxyError(f"{name} must not contain symlinks")
    return path


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ProxyError("upstream port must be an integer between 1 and 65535")
    return value


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=30)
        self.path = path

    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.timeout)
            connection.connect(str(self.path))
            # Linux is the supported deployment host. Filesystem ownership alone
            # cannot identify a process that inherited an already opened socket.
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != os.getuid():
                raise ProxyError("admin socket peer is not owned by the deployment user")
            self.sock = connection
        except BaseException:
            connection.close()
            raise


class DeploymentProxy:
    """An owner-private proxy; config is a mapping, not executable configuration."""

    def __init__(self, config, state_dir):
        if not isinstance(config, dict) or set(config) - {
            "caddy",
            "origin",
            "bind",
            "tls_cert",
            "tls_key",
        }:
            raise ProxyError("unsupported proxy configuration keys")
        self.caddy = _path(config.get("caddy"), "caddy")
        if not self.caddy.is_file() or not os.access(self.caddy, os.X_OK):
            raise ProxyError("caddy must be an existing executable file")
        self.state_dir = _path(state_dir, "state_dir")
        self.config_path = self.state_dir / "Caddyfile"
        self.admin_socket = self.state_dir / "admin.sock"
        if len(os.fsencode(self.admin_socket)) > 107:
            raise ProxyError("state_dir is too long for a Unix admin socket")
        self.admin_address = "unix/" + str(self.admin_socket)
        self.server_name = (
            "praximesh_" + hashlib.sha256(os.fsencode(self.state_dir)).hexdigest()[:16]
        )
        raw_origin = config.get("origin")
        if not isinstance(raw_origin, str) or not re.fullmatch(
            r"https?://[A-Za-z0-9.:[\]-]+", raw_origin
        ):
            raise ProxyError("origin must be an http(s) URL without path, credentials or query")
        try:
            origin = urlsplit(raw_origin)
            self.port = origin.port or (443 if origin.scheme == "https" else 80)
            hostname = origin.hostname
            if not hostname or origin.port == 0 or origin.netloc.endswith(":"):
                raise ValueError("missing host or invalid port")
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname):
                    raise ValueError("invalid hostname") from None
                if any(
                    not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                    for label in hostname.split(".")
                ):
                    raise ValueError("invalid hostname") from None
            self.bind = str(ipaddress.ip_address(config.get("bind", "127.0.0.1")))
        except (ValueError, TypeError) as error:
            raise ProxyError("invalid proxy origin, port or bind IP") from error
        if origin.scheme == "http" and not (_loopback(hostname) and _loopback(self.bind)):
            raise ProxyError("HTTP requires a loopback origin and loopback bind IP")
        self.origin, self.scheme = raw_origin, origin.scheme
        listen_host = f"[{self.bind}]" if ":" in self.bind else self.bind
        self.listen = f"{listen_host}:{self.port}"
        cert, key = config.get("tls_cert"), config.get("tls_key")
        if bool(cert) != bool(key) or (cert and self.scheme != "https"):
            raise ProxyError("TLS certificate and key must be paired and require HTTPS")
        self.tls_cert = _path(cert, "tls_cert") if cert else None
        self.tls_key = _path(key, "tls_key") if key else None
        for path in (self.tls_cert, self.tls_key):
            if path is not None and not path.is_file():
                raise ProxyError("TLS certificate and key must be existing regular files")
        self._check_directory(create=False, missing_ok=True)

    def _check_directory(self, *, create=False, missing_ok=False):
        _path(self.state_dir, "state_dir")
        if create and not self.state_dir.exists():
            self.state_dir.mkdir(mode=0o700)
            parent = os.open(self.state_dir.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        try:
            details = self.state_dir.lstat()
        except FileNotFoundError:
            if missing_ok:
                return
            raise ProxyError("private proxy state directory has not been prepared") from None
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise ProxyError("proxy state directory must be owned by current uid with mode 0700")
        for name in ("data", "config", "storage"):
            path = _path(self.state_dir / name, "private Caddy storage")
            if path.exists():
                details = path.lstat()
                if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
                    raise ProxyError("Caddy storage must remain in owned private directories")

    def environment(self):
        """A minimal process environment with storage wholly below state_dir."""
        return {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "XDG_DATA_HOME": str(self.state_dir / "data"),
            "XDG_CONFIG_HOME": str(self.state_dir / "config"),
        }

    def ensure_directory(self):
        """Create/validate private storage before the caller acquires its lock."""
        self._check_directory(create=True)
        return self.state_dir

    def command(self):
        self._check_directory()
        self._read_restart()
        if self.admin_socket.exists() or self.admin_socket.is_symlink():
            self._remove_stale_socket()
        return [str(self.caddy), "run", "--config", str(self.config_path), "--adapter", "caddyfile"]

    def _remove_stale_socket(self):
        """Recover only an owned, unchanged socket left behind by a dead process."""
        _path(self.admin_socket, "admin socket")
        original = self.admin_socket.lstat()
        if not stat.S_ISSOCK(original.st_mode) or original.st_uid != os.getuid():
            raise ProxyError("refuse to replace an unknown admin endpoint")
        connection = _UnixHTTP(self.admin_socket)
        try:
            connection.connect()
        except ConnectionRefusedError as error:
            if error.errno != errno.ECONNREFUSED:
                raise ProxyError("cannot establish that the admin socket is stale") from error
        except OSError as error:
            raise ProxyError("cannot establish that the admin socket is stale") from error
        else:
            raise ProxyError("refuse to replace a running proxy")
        finally:
            connection.close()
        self._check_directory()
        _path(self.admin_socket, "admin socket")
        current = self.admin_socket.lstat()
        if (original.st_dev, original.st_ino, original.st_mode, original.st_uid) != (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_uid,
        ):
            raise ProxyError("admin socket changed while checking its ownership")
        self.admin_socket.unlink()
        directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _render(self, port=None):
        if port is not None:
            _port(port)
        lines = [
            "{",
            f'    admin "{self.admin_address}"',
            "    persist_config off",
            "    skip_install_trust",
            "    auto_https disable_redirects",
            "    storage file_system {",
            f'        root "{self.state_dir}/storage"',
            "    }",
            f"    servers {self.listen} {{",
            f"        name {self.server_name}",
            "        protocols h1 h2",
            "        max_header_size 32KB",
            "        timeouts {",
            "            read_header 10s",
            "            read_body 30s",
            "            write 30s",
            "            idle 120s",
            "        }",
            "    }",
            "}",
            f"{self.origin} {{",
            f"    bind {self.bind}",
        ]
        if self.tls_cert:
            lines.append(f'    tls "{self.tls_cert}" "{self.tls_key}"')
        lines.extend(["    request_body {", "        max_size 100KB", "    }"])
        if port is None:
            lines.extend(
                [
                    "    header Cache-Control no-store",
                    "    header Retry-After 60",
                    '    respond "PraxiMesh is temporarily unavailable. Please try again shortly." 503',
                ]
            )
        else:
            lines.extend(
                [
                    f"    reverse_proxy 127.0.0.1:{port} {{",
                    "        header_up X-Forwarded-Proto {scheme}",
                    "        header_up X-Real-IP {remote_host}",
                    "    }",
                ]
            )
        return "\n".join([*lines, "}", ""])

    def _adapt(self, source, *, validate=False):
        _path(self.caddy, "caddy")
        for path in (self.tls_cert, self.tls_key):
            if path:
                _path(path, "TLS file")
        command = [str(self.caddy), "adapt", "--config", "-", "--adapter", "caddyfile"]
        if validate:
            command.append("--validate")
        try:
            result = subprocess.run(
                command,
                input=source.encode(),
                capture_output=True,
                check=True,
                timeout=30,
                env=self.environment(),
            )
            if len(result.stdout) > MAX_CONFIG_BYTES:
                raise ProxyError("adapted proxy configuration is too large")
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError("configuration must be an object")
            return value
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ProxyError("Caddy configuration validation failed") from error

    def _read_restart(self):
        self._check_directory()
        _path(self.config_path, "restart configuration")
        try:
            details = self.config_path.lstat()
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) != 0o600
                or details.st_size > MAX_CONFIG_BYTES
            ):
                raise ProxyError("restart configuration must be a private owned regular file")
            source = self.config_path.read_text()
        except (OSError, UnicodeError) as error:
            raise ProxyError("cannot read private restart configuration") from error
        match = re.search(r"^    reverse_proxy 127\.0\.0\.1:(\d+) \{$", source, re.MULTILINE)
        port = _port(int(match[1])) if match else None
        if source != self._render(port):
            raise ProxyError("restart configuration is not the exact owned configuration")
        return source

    def _persist(self, source):
        self._check_directory()
        if self.config_path.exists() or self.config_path.is_symlink():
            self._read_restart()
        descriptor, name = tempfile.mkstemp(prefix=".Caddyfile-", dir=self.state_dir)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(source.encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.config_path)
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            path.unlink(missing_ok=True)

    def prepare(self):
        """Create the initial maintenance file; never reset an existing activation."""
        self._check_directory(create=True)
        if self.admin_socket.exists() or self.admin_socket.is_symlink():
            # An existing endpoint may only be inspected, never overwritten by
            # an initial configuration or a replacement Caddy process.
            self._read_restart()
            self.probe()
        source = self._read_restart() if self.config_path.exists() else self._render()
        self._adapt(source, validate=True)
        if not self.config_path.exists():
            self._persist(source)
        return self.config_path

    def _request(self, method, path, body=None, headers=None):
        self._check_directory()
        _path(self.admin_socket, "admin socket")
        try:
            details = self.admin_socket.lstat()
            if not stat.S_ISSOCK(details.st_mode) or details.st_uid != os.getuid():
                raise ProxyError("admin endpoint is not an owned Unix socket")
            connection = _UnixHTTP(self.admin_socket)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                data = response.read(MAX_CONFIG_BYTES + 1)
                if len(data) > MAX_CONFIG_BYTES:
                    raise ProxyError("admin response exceeds configuration limit")
                if response.status != 200:
                    raise ProxyError(f"Caddy admin request failed with HTTP {response.status}")
                return data, {key.lower(): value for key, value in response.getheaders()}
            finally:
                connection.close()
        except (OSError, http.client.HTTPException) as error:
            raise ProxyError("cannot contact the dedicated Caddy Unix admin socket") from error

    def _current(self):
        data, headers = self._request("GET", "/config/")
        try:
            actual = json.loads(data)
            admin = actual["admin"]["listen"]
            servers = actual["apps"]["http"]["servers"]
            if admin != self.admin_address or set(servers) != {self.server_name}:
                raise ValueError("identity mismatch")
            proxies = [item for item in _objects(actual) if item.get("handler") == "reverse_proxy"]
            port = None
            if proxies:
                if len(proxies) != 1 or len(proxies[0]["upstreams"]) != 1:
                    raise ValueError("unexpected upstreams")
                dial = proxies[0]["upstreams"][0]["dial"]
                if not isinstance(dial, str) or not re.fullmatch(r"127\.0\.0\.1:[0-9]+", dial):
                    raise ValueError("unexpected upstream address")
                port = _port(int(dial.rsplit(":", 1)[1]))
            if actual != self._adapt(self._render(port)):
                raise ValueError("configuration mismatch")
            etag = headers.get("etag")
            if not etag or len(etag) > 256 or "\r" in etag or "\n" in etag:
                raise ValueError("missing configuration ETag")
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise ProxyError(
                "running proxy does not match the exact owned configuration"
            ) from error
        return {
            "mode": "maintenance" if port is None else "active",
            "upstream": None if port is None else f"127.0.0.1:{port}",
            "port": port,
            "origin": self.origin,
            "admin_socket": str(self.admin_socket),
        }, etag

    def status(self):
        """Read and authenticate actual running state; the restart file is not evidence."""
        return self._current()[0]

    def probe(self):
        return self.status()

    def _transition(self, port):
        source = self._render(port)
        candidate = self._adapt(source, validate=True)
        # Compare-and-swap protects the ownership inspection from concurrent edits.
        _, etag = self._current()
        self._persist(source)
        self._request(
            "POST",
            "/config/",
            body=json.dumps(candidate).encode(),
            headers={"Content-Type": "application/json", "If-Match": etag},
        )
        observed = self.status()
        if observed["port"] != port:
            raise ProxyError("live proxy did not confirm the requested configuration")
        return observed

    def maintenance(self):
        return self._transition(None)

    def activate(self, port):
        """Caller must durably record exposure intent before invoking this method."""
        return self._transition(_port(port))
