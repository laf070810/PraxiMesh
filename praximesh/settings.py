"""Local defaults never enable execution. Production configuration fails closed."""

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
PRODUCTION = os.environ.get("PRAXIMESH_MODE", "local") == "production"
DEBUG = not PRODUCTION and os.environ.get("APP_DEBUG", "1") == "1"
SECRET_KEY = os.environ.get("APP_SECRET_KEY", "local-development-only-not-for-public-use")
ALLOWED_HOSTS = os.environ.get("APP_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
if PRODUCTION and (len(SECRET_KEY) < 50 or SECRET_KEY.startswith("local-") or "*" in ALLOWED_HOSTS):
    raise ImproperlyConfigured("Production requires a random APP_SECRET_KEY and explicit hosts.")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "learning",
    "community",
    "practice_api",
    "showcase",
    "localization",
    "projects",
    "organizations",
    "coaching",
    "challenges",
]
MIDDLEWARE = [
    "core.middleware.ProxyHeadersMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.SecurityPolicyMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "praximesh.urls"
WSGI_APPLICATION = "praximesh.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
database_url = os.environ.get("DATABASE_URL")
if database_url:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL must use PostgreSQL.")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname,
            "PORT": parsed.port or 5432,
            "OPTIONS": {"sslmode": os.environ.get("DB_SSLMODE", "require")},
        }
    }
else:
    if PRODUCTION:
        raise ImproperlyConfigured("Production requires DATABASE_URL (PostgreSQL).")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_PATH", BASE_DIR / "db.sqlite3"),
            "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "home"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = PRODUCTION
CSRF_COOKIE_SECURE = PRODUCTION
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7
SECURE_SSL_REDIRECT = PRODUCTION
SECURE_HSTS_SECONDS = 31536000 if PRODUCTION else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = PRODUCTION
SECURE_HSTS_PRELOAD = PRODUCTION
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# The first middleware additionally checks peer IP and requires one protocol.
# Direct backend access must still be blocked and the proxy must overwrite it.
if os.environ.get("TRUST_TLS_PROXY") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [x for x in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if x]
DATA_UPLOAD_MAX_MEMORY_SIZE = 100_000
DATA_UPLOAD_MAX_NUMBER_FIELDS = 30
LANGUAGE_CODE = "zh-hans"
LANGUAGES = [("zh-hans", "简体中文"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "locale"]
LANGUAGE_COOKIE_SECURE = PRODUCTION
LANGUAGE_COOKIE_HTTPONLY = True
LANGUAGE_COOKIE_SAMESITE = "Lax"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("APP_STATIC_ROOT", BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "0" if PRODUCTION else "1") == "1"
TRUSTED_PROXY_IPS = [value for value in os.environ.get("TRUSTED_PROXY_IPS", "").split(",") if value]
if os.environ.get("TRUST_TLS_PROXY") == "1" and not TRUSTED_PROXY_IPS:
    raise ImproperlyConfigured("TRUST_TLS_PROXY requires explicit TRUSTED_PROXY_IPS.")
JUDGE_ENABLED = os.environ.get("JUDGE_ENABLED", "0") == "1"
JUDGE_BACKEND = os.environ.get("JUDGE_BACKEND", "gvisor")
JUDGE_IMAGE = os.environ.get("JUDGE_IMAGE", "")
JUDGE_WASI_BUNDLE = os.environ.get("JUDGE_WASI_BUNDLE", "")
JUDGE_WASI_SHA256 = os.environ.get("JUDGE_WASI_SHA256", "")
JUDGE_RECEIPT = os.environ.get("JUDGE_RECEIPT", str(BASE_DIR / "var/sandbox-receipt.json"))
JUDGE_MAX_PENDING = 2
JUDGE_MAX_QUEUE = 100
JUDGE_DAILY_CPU_UNITS = int(os.environ.get("JUDGE_DAILY_CPU_UNITS", "120"))
if not 1 <= JUDGE_DAILY_CPU_UNITS <= 10000:
    raise ImproperlyConfigured("JUDGE_DAILY_CPU_UNITS must be between 1 and 10000.")
JUDGE_LEASE_SECONDS = 180
CONTENT_ROOT = BASE_DIR / "content"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# Optional, explicitly configured text-only tutoring. Never enables judging.
COACHING_ENABLED = os.environ.get("COACHING_ENABLED", "0") == "1"
COACHING_ENDPOINT = os.environ.get("COACHING_ENDPOINT", "")
COACHING_MODEL = os.environ.get("COACHING_MODEL", "")
COACHING_TOKEN = os.environ.get("COACHING_TOKEN", "")
COACHING_PROVIDER_DESCRIPTION = os.environ.get("COACHING_PROVIDER_DESCRIPTION", "")
COACHING_ALLOW_LOOPBACK_HTTP = os.environ.get("COACHING_ALLOW_LOOPBACK_HTTP", "0") == "1"
COACHING_CA_FILE = os.environ.get("COACHING_CA_FILE", "")
COACHING_CA_PEM = os.environ.get("COACHING_CA_PEM", "")
COACHING_DISABLE_THINKING = os.environ.get("COACHING_DISABLE_THINKING", "0") == "1"
COACHING_DAILY_LIMIT = int(os.environ.get("COACHING_DAILY_LIMIT", "5"))
COACHING_CONCURRENCY = int(os.environ.get("COACHING_CONCURRENCY", "1"))
COACHING_MAX_TOKENS = int(os.environ.get("COACHING_MAX_TOKENS", "192"))
