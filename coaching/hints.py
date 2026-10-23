"""Explicit previews, bounded reservations and no-resurrection completion writes."""

import json
from datetime import UTC, timedelta

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from django.utils.translation import get_language, gettext

from coaching import configuration
from coaching.models import DailyUsage, ExecutionLease, HintRequest, SlotLock
from coaching.provider import MAX_PROMPT_BYTES, ProviderError, request_hint
from core.content import digest
from core.models import ProblemVersion
from localization.presentation import localized_problem


class HintError(ValueError):
    pass


def encode(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def checksum(payload, instructions, identity):
    return digest({"payload": payload, "instructions": instructions, "configuration": identity})


def _visible_version(version_id):
    try:
        version = ProblemVersion.objects.select_related("problem").get(
            pk=version_id, problem__published=True, problem__current__isnull=False
        )
    except ProblemVersion.DoesNotExist:
        raise Http404 from None
    if digest(version.data) != version.digest:
        raise Http404
    return version


@transaction.atomic
def preview(user, version_id, *, question, hint_level=0, include_source=False, source=""):
    config = configuration.current()
    version = _visible_version(version_id)
    if (
        not user.is_authenticated
        or not isinstance(question, str)
        or not 1 <= len(question.strip()) <= 1000
        or "\0" in question
        or type(hint_level) is not int
        or type(include_source) is not bool
        or not isinstance(source, str)
        or len(source) > 3000
        or "\0" in source
    ):
        raise HintError("invalid_preview")
    public = localized_problem(version)
    if not 0 <= hint_level <= len(public["hints"]):
        raise HintError("invalid_preview")
    if include_source and not source.strip():
        raise HintError("empty_source")
    if HintRequest.objects.filter(user=user, state=HintRequest.State.PREVIEW).count() >= 20:
        raise HintError("too_many_previews")
    safe = {
        key: public[key]
        for key in [
            "id",
            "revision",
            "title",
            "summary",
            "statement",
            "constraints",
            "outcomes",
            "examples",
            "language",
            "evaluator",
            "comparison",
        ]
    }
    if "function" in public:
        safe["function"] = public["function"]
    if "interface" in public:
        safe["interface"] = public["interface"]
    payload = {
        "language": "English" if (get_language() or "").startswith("en") else "简体中文",
        "problem": safe,
        "problem_digest": version.digest,
        "question": question.strip(),
        "selected_author_hints": public["hints"][:hint_level],
        "source_opt_in": include_source,
    }
    if include_source:
        payload["learner_source"] = source
    try:
        if len(encode(payload).encode()) > MAX_PROMPT_BYTES:
            raise HintError("preview_too_large")
    except (UnicodeError, ValueError):
        raise HintError("invalid_preview") from None
    return HintRequest.objects.create(
        user=user,
        version=version,
        payload=payload,
        instructions=configuration.INSTRUCTIONS,
        request_digest=checksum(payload, configuration.INSTRUCTIONS, config.identity),
        configuration_digest=config.identity,
        provider_description=config.description,
        model=config.provider.model,
        prompt_version=configuration.PROMPT_VERSION,
    )


def owned(user, pk):
    item = HintRequest.objects.filter(pk=pk, user=user).select_related("version__problem").first()
    if item is None:
        raise Http404
    return item


@transaction.atomic
def _reserve(user, pk, expected_digest, config, now):
    try:
        SlotLock.objects.select_for_update().get(pk=1)
    except SlotLock.DoesNotExist:
        raise HintError("unavailable") from None
    HintRequest.objects.filter(state=HintRequest.State.RUNNING, lease_until__lte=now).update(
        state=HintRequest.State.FAILED, error_code="interrupted", finished_at=now, lease_until=None
    )
    ExecutionLease.objects.filter(expires_at__lte=now).delete()
    item = HintRequest.objects.select_for_update(of=("self",)).filter(pk=pk, user=user).first()
    if item is None:
        raise Http404
    if item.request_digest != expected_digest:
        raise HintError("preview_changed")
    if item.state != HintRequest.State.PREVIEW:
        return item, False
    _visible_version(item.version_id)
    if (
        item.configuration_digest != config.identity
        or item.request_digest != checksum(item.payload, item.instructions, config.identity)
        or item.instructions != configuration.INSTRUCTIONS
    ):
        raise HintError("preview_changed")
    if ExecutionLease.objects.count() >= config.concurrency:
        raise HintError("busy")
    day = now.astimezone(UTC).date()
    usage, _created = DailyUsage.objects.get_or_create(user=user, day=day)
    if usage.count >= config.daily_limit:
        raise HintError("quota")
    usage.count += 1
    usage.save(update_fields=["count"])
    item.state = HintRequest.State.RUNNING
    item.charged_day = day
    item.lease_until = now + timedelta(seconds=config.provider.timeout_seconds + 5)
    ExecutionLease.objects.create(id=item.pk, expires_at=item.lease_until)
    item.save(update_fields=["state", "charged_day", "lease_until"])
    return item, True


def send(user, pk, expected_digest):
    config = configuration.current()
    item, reserved = _reserve(user, pk, expected_digest, config, timezone.now())
    if not reserved:
        return item
    # The bounded provider runs outside the transaction. Even a lost response
    # consumes budget; deletion never recreates the request or the account.
    try:
        result = request_hint(
            config.provider, instructions=item.instructions, prompt=encode(item.payload)
        )
        values = {
            "state": HintRequest.State.COMPLETE,
            "response": result.text,
            "truncated": result.truncated,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "elapsed_ms": result.elapsed_ms,
            "error_code": "",
        }
    except ProviderError as error:
        safe = (
            error.code
            if error.code
            in {
                "timeout",
                "invalid_response",
                "network_error",
                "http_error",
                "response_too_large",
                "unavailable",
            }
            else "unavailable"
        )
        values = {"state": HintRequest.State.FAILED, "error_code": safe}
    except Exception:
        # No exception body, endpoint response, source text or credential enters
        # application messages or persisted safe failure metadata.
        values = {"state": HintRequest.State.FAILED, "error_code": "unavailable"}
    ExecutionLease.objects.filter(pk=item.pk).delete()
    changed = HintRequest.objects.filter(
        pk=pk, user_id=user.pk, state=HintRequest.State.RUNNING
    ).update(**values, lease_until=None, finished_at=timezone.now())
    return owned(user, pk) if changed else None


def remove(user, pk):
    item = owned(user, pk)
    item.delete()


def usage(user):
    row = DailyUsage.objects.filter(user=user, day=timezone.now().astimezone(UTC).date()).first()
    return row.count if row else 0


def display(item):
    # Retained private IDs/digests remain available after withdrawal; a hidden
    # problem's copied statement or model response must not restore its body.
    available = item.version.problem.published and item.version.problem.current_id is not None
    expired = (
        item.state == HintRequest.State.RUNNING
        and item.lease_until is not None
        and item.lease_until <= timezone.now()
    )
    return {
        "state_label": gettext("辅导暂不可用") if expired else item.get_state_display(),
        "effective_state": HintRequest.State.FAILED if expired else item.state,
        "item": item,
        "content_available": available,
        "preview_json": encode(item.payload) if available else "",
        "response": item.response if available else "",
    }
