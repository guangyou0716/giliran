"""Django settings for giliran.

Configuration comes from environment variables (see .env.example). Defaults are
for local development only: DEBUG on, SQLite, and an insecure development key.
"""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = env_bool("DJANGO_DEBUG", True)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off")
    SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"  # noqa: S105

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "queues",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# An unset or empty DATABASE_URL means local SQLite.
DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=60,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", "English"),
    ("ms", "Bahasa Melayu"),
    ("zh-hans", "简体中文"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Asia/Kuala_Lumpur"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "staff:dashboard"
LOGOUT_REDIRECT_URL = "home"

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# How often (milliseconds) the ticket page and display board poll for updates.
GILIRAN_POLL_MS = int(os.environ.get("GILIRAN_POLL_MS", "5000"))

# Live updates by Server-Sent Events (queues/streams.py); pages fall back to polling.
# Each open stream holds one worker thread for up to GILIRAN_SSE_MAX_SECONDS, so run
# gunicorn with threads (e.g. --worker-class gthread --threads 32) or set GILIRAN_SSE=0
# to use polling only.
GILIRAN_SSE = os.environ.get("GILIRAN_SSE", "1") not in ("0", "false", "False", "")
GILIRAN_SSE_MAX_SECONDS = float(os.environ.get("GILIRAN_SSE_MAX_SECONDS", "60"))
GILIRAN_SSE_TICK_SECONDS = float(os.environ.get("GILIRAN_SSE_TICK_SECONDS", "1.5"))
GILIRAN_SSE_HEARTBEAT_SECONDS = float(os.environ.get("GILIRAN_SSE_HEARTBEAT_SECONDS", "15"))
GILIRAN_SSE_REFRESH_SECONDS = float(os.environ.get("GILIRAN_SSE_REFRESH_SECONDS", "10"))
# The ticket page shows "almost your turn" (and plays the chime) at this many people ahead.
GILIRAN_ALMOST_AHEAD = int(os.environ.get("GILIRAN_ALMOST_AHEAD", "2"))

# Appointments (see queues/engine.py "Appointments").
# Minutes after a slot starts during which a booked customer can still check in on time
# (and still book it); later bookings become no-shows.
GILIRAN_BOOKING_GRACE_MIN = int(os.environ.get("GILIRAN_BOOKING_GRACE_MIN", "10"))
# Customers can check themselves in from this many minutes before their slot.
GILIRAN_CHECKIN_EARLY_MIN = int(os.environ.get("GILIRAN_CHECKIN_EARLY_MIN", "30"))
# How many days ahead (including today) the booking page offers.
GILIRAN_BOOKING_DAYS = int(os.environ.get("GILIRAN_BOOKING_DAYS", "7"))
