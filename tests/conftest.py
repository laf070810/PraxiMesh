import pytest
from django.conf import settings

from core.content import import_package, load_package


@pytest.fixture(scope="session", autouse=True)
def static_manifest(tmp_path_factory):
    from django.core.management import call_command
    from django.test import override_settings

    with override_settings(STATIC_ROOT=tmp_path_factory.mktemp("static")):
        call_command("collectstatic", interactive=False, verbosity=0)
        yield


@pytest.fixture
def package():
    return load_package(settings.CONTENT_ROOT / "starter.json")


@pytest.fixture
def catalog(db, package):
    from organizations.models import PublicationLock

    # Transactional database tests flush data migrations between cases.
    # Restore the migration-owned fixture; production import still fails closed
    # when its required lock is absent.
    PublicationLock.objects.get_or_create(pk=1)
    import_package(package)
    return package


@pytest.fixture
def user(db, django_user_model):
    return django_user_model.objects.create_user(
        username="learner", password="Test-passphrase-9621"
    )


@pytest.fixture
def other_user(db, django_user_model):
    return django_user_model.objects.create_user(username="other", password="Other-passphrase-9521")


@pytest.fixture
def logged_client(client, user):
    client.force_login(user)
    return client
