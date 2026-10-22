import hashlib
import hmac
import time
from datetime import UTC, datetime

from django.conf import settings
from django.db.models import F

from core.models import RateBucket


def consume(scope: str, subject: str, limit: int, seconds: int) -> bool:
    """Atomic fixed-window rate limit shared by all application processes.

    At most 2*limit is possible across a window boundary. Pending queue limits
    provide the separate resource bound. Raw IPs and recovery tokens aren't stored.
    """
    window = int(time.time()) // seconds
    key = hmac.new(
        settings.SECRET_KEY.encode(), f"{scope}:{subject}:{window}".encode(), hashlib.sha256
    ).hexdigest()
    RateBucket.objects.get_or_create(
        key=key, defaults={"expires_at": datetime.fromtimestamp((window + 1) * seconds, UTC)}
    )
    return bool(RateBucket.objects.filter(key=key, count__lt=limit).update(count=F("count") + 1))
