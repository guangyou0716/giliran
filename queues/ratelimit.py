"""Per-IP rate limit for the public endpoints that create data.

``@rate_limited("join")`` counts POST requests per client IP in the Django cache
(fixed window of ``GILIRAN_RATE_LIMIT_WINDOW`` seconds, ``GILIRAN_RATE_LIMIT_MAX``
requests) and answers 429 with a friendly page and a ``Retry-After`` header once the
limit is passed. GET requests (viewing a page) are never limited. ``GILIRAN_RATE_LIMIT_MAX=0``
turns the limit off.

The default cache is process-local memory, so the limit is per worker process: with N
worker processes a client can make up to N times the limit. The Docker image runs one
process with many threads (needed for the live streams anyway); behind several processes
configure a shared cache backend (``CACHES``). Behind a reverse proxy set
``GILIRAN_TRUST_PROXY=1`` so the real client address (the last ``X-Forwarded-For`` entry,
the one your proxy added) is used; never enable it when clients can reach the app directly,
because they could then pick their own address.
"""

import hashlib
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render


def client_ip(request) -> str:
    if settings.GILIRAN_TRUST_PROXY:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _key(scope: str, ip: str) -> str:
    return f"giliran:rl:{scope}:{hashlib.sha256(ip.encode()).hexdigest()[:32]}"


def over_limit(request, scope: str) -> bool:
    """Count this request; True when the client is past the limit."""
    limit = settings.GILIRAN_RATE_LIMIT_MAX
    if limit <= 0:
        return False
    window = max(settings.GILIRAN_RATE_LIMIT_WINDOW, 1)
    key = _key(scope, client_ip(request))
    cache.add(key, 0, timeout=window)  # starts the window; keeps the original expiry
    try:
        count = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, timeout=window)
        count = 1
    return count > limit


def rate_limited(scope: str):
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method == "POST" and over_limit(request, scope):
                response = render(
                    request,
                    "queues/rate_limited.html",
                    {"window": settings.GILIRAN_RATE_LIMIT_WINDOW},
                    status=429,
                )
                response["Retry-After"] = str(settings.GILIRAN_RATE_LIMIT_WINDOW)
                return response
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
