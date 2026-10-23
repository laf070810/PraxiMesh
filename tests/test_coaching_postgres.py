"""Actual PostgreSQL reservation/deletion races; no model-quality claim."""

import queue
import threading
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse

from coaching import hints
from coaching.models import DailyUsage, ExecutionLease, HintRequest, SlotLock
from tests.test_coaching import preview, result

pytest_plugins = ["tests.test_coaching"]
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def postgres_only():
    if connection.vendor != "postgresql":
        pytest.skip("Requires isolated PostgreSQL")
    SlotLock.objects.get_or_create(pk=1)


def test_inflight_request_is_idempotent_and_delete_does_not_reopen_slot(
    user, other_user, version, configured
):
    first, second = preview(user, version), preview(other_user, version)
    entered, release = threading.Event(), threading.Event()
    output = queue.Queue()

    def provider(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return result()

    def run():
        close_old_connections()
        try:
            output.put(hints.send(user, first.pk, first.request_digest))
        except Exception as error:
            output.put(error)
        finally:
            connection.close()

    with patch("coaching.hints.request_hint", side_effect=provider) as transport:
        worker = threading.Thread(target=run)
        worker.start()
        try:
            assert entered.wait(10)
            assert (
                hints.send(user, first.pk, first.request_digest).state == HintRequest.State.RUNNING
            )
            with pytest.raises(hints.HintError, match="busy"):
                hints.send(other_user, second.pk, second.request_digest)
            client = Client()
            client.force_login(user)
            assert (
                client.post(
                    reverse("delete_account"),
                    {"password": "Test-passphrase-9621", "confirm": "DELETE"},
                ).status_code
                == 200
            )
            assert ExecutionLease.objects.filter(pk=first.pk).exists()
            with pytest.raises(hints.HintError, match="busy"):
                hints.send(other_user, second.pk, second.request_digest)
        finally:
            release.set()
            worker.join(15)
            assert not worker.is_alive()
        assert output.get_nowait() is None
        assert transport.call_count == 1
    assert not HintRequest.objects.filter(pk=first.pk).exists()
    assert not DailyUsage.objects.filter(user_id=user.pk).exists()
    assert not ExecutionLease.objects.exists()
