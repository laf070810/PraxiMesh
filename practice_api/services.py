import hashlib
import json
import re
import secrets
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from core.models import Problem
from core.queue import AdmissionError, enqueue, lock_queue
from practice_api.models import AccessToken, SubmissionRequest

TOKEN = re.compile(r"pm_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})")


def secret_digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


@transaction.atomic
def issue_token(user, label, days, *, can_submit, can_read_results):
    if not user.is_active or not label.strip() or len(label) > 100:
        raise ValueError("Invalid account or token label")
    if type(days) is not int or not 1 <= days <= 90 or not (can_submit or can_read_results):
        raise ValueError("Choose at least one permission and an expiry of 1–90 days")
    get_user_model().objects.select_for_update().get(pk=user.pk)
    now = timezone.now()
    if AccessToken.objects.filter(user=user, revoked_at=None, expires_at__gt=now).count() >= 10:
        raise ValueError("At most ten active tokens are allowed")
    identity = uuid.uuid4()
    raw = f"pm_{identity.hex}.{secrets.token_urlsafe(32)}"
    token = AccessToken.objects.create(
        id=identity,
        user=user,
        label=label.strip(),
        secret_digest=secret_digest(raw),
        can_submit=can_submit,
        can_read_results=can_read_results,
        expires_at=now + timedelta(days=days),
    )
    return token, raw


def authenticate(raw):
    match = TOKEN.fullmatch(raw)
    if not match:
        return None
    token = (
        AccessToken.objects.select_related("user")
        .filter(
            pk=uuid.UUID(hex=match[1]),
            revoked_at=None,
            expires_at__gt=timezone.now(),
            user__is_active=True,
        )
        .first()
    )
    if token and secrets.compare_digest(token.secret_digest, secret_digest(raw)):
        return token
    return None


@transaction.atomic
def submit_once(user, key, slug, version_id, source, mode):
    # Serialize with ordinary admission; an idempotent replay never creates or
    # charges for a second job, even when the matching worker is now unavailable.
    lock_queue()
    payload = json.dumps(
        [slug, version_id, source, mode], ensure_ascii=False, separators=(",", ":")
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    previous = (
        SubmissionRequest.objects.select_related("submission").filter(user=user, key=key).first()
    )
    if previous:
        if not secrets.compare_digest(previous.request_digest, digest):
            raise AdmissionError(
                "The idempotency key was already used with a different request.", 409
            )
        return previous.submission, False
    problem = Problem.objects.select_related("current").filter(pk=slug, published=True).first()
    if not problem or not problem.current_id:
        raise AdmissionError("Problem is not available.", 404)
    if problem.current_id != version_id:
        raise AdmissionError(
            "Problem version changed. Download the current task before submitting.", 409
        )
    from core.limits import consume

    if not consume("submit", str(user.pk), 12, 60):
        raise AdmissionError("Submission rate exceeded.", 429)
    job = enqueue(user, problem.current, source, mode)
    SubmissionRequest.objects.create(user=user, key=key, request_digest=digest, submission=job)
    return job, True


def export_user_data(user):
    return {
        "tokens": [
            {
                "id": str(token.pk),
                "label": token.label,
                "can_submit": token.can_submit,
                "can_read_results": token.can_read_results,
                "created_at": token.created_at.isoformat(),
                "expires_at": token.expires_at.isoformat(),
                "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
            }
            for token in AccessToken.objects.filter(user=user)
        ],
        "submission_requests": [
            {
                "key": str(row.key),
                "submission": str(row.submission_id),
                "created_at": row.created_at.isoformat(),
            }
            for row in SubmissionRequest.objects.filter(user=user)
        ],
    }
