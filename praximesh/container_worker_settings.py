"""Fixed worker configuration; no Web secret or caller environment is inherited."""

import os
import secrets

from judge.container_worker_config import database_settings, read_config

WORKER_CONFIGURATION, WORKER_GENERATION = read_config(with_generation=True)
os.environ.clear()
os.environ.update(
    {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PRAXIMESH_MODE": "production",
        "APP_SECRET_KEY": secrets.token_urlsafe(64),
        "APP_ALLOWED_HOSTS": "localhost",
        "DATABASE_URL": WORKER_CONFIGURATION["database"]["url"],
        "DB_SSLMODE": WORKER_CONFIGURATION["database"]["sslmode"],
        "JUDGE_ENABLED": "1" if WORKER_CONFIGURATION["enabled"] else "0",
        "JUDGE_GPU_DEVICES": WORKER_CONFIGURATION["gpu"],
    }
)

from praximesh.settings import *  # noqa: E402,F403

DATABASES = {"default": database_settings(WORKER_CONFIGURATION)}
# The shared settings have already parsed the private URL. Do not propagate it
# even to a future trusted subprocess that happens to inherit the environment.
os.environ.pop("DATABASE_URL", None)
os.environ.pop("APP_SECRET_KEY", None)
