"""Public CUDA downloads/discovery only; no driver, compiler or GPU execution."""

import copy
import stat
import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.urls import reverse
from django.utils import timezone

from scripts import practice
from tests.gpu_evidence_fixtures import version_fixture
from tests.test_gpu_admission import make_device

pytestmark = pytest.mark.django_db


@pytest.fixture
def gpu_package(client):
    version = version_fixture()
    response = client.get(reverse("practice_api:problem", args=[version.problem_id]))
    assert response.status_code == 200
    package = response.json()
    assert "PRIVATE_REFERENCE_SENTINEL" not in response.content.decode()
    assert "PROTECTED_CASE_SENTINEL" not in response.content.decode()
    return package


def test_gpu_package_downloads_private_source_and_preserves_it_without_local_execution(
    gpu_package, tmp_path, monkeypatch
):
    client = practice.Client("https://example.test")
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: gpu_package)
    directory = tmp_path / "cuda-work"
    result = practice.download(client, gpu_package["problem"]["id"], directory)
    assert result["solution"] == "solution.cu"
    assert stat.S_IMODE((directory / "solution.cu").stat().st_mode) == 0o600
    source = '// Private local edit\nextern "C" __global__ void solve() {}'
    (directory / "solution.cu").write_text(source)
    # Even sandbox construction must not be attempted for this local command.
    sandbox = Mock(side_effect=AssertionError("WASI cannot evaluate CUDA"))
    monkeypatch.setattr("judge.wasi.WasiSandbox", sandbox)
    with pytest.raises(practice.PracticeError, match="CUDA public tests cannot run in WASI"):
        practice.local_test(directory, tmp_path / "absent", "a" * 64)
    sandbox.assert_not_called()
    assert practice.load_exercise(directory) == (client.origin, gpu_package, source)
    with pytest.raises(practice.PracticeError):
        practice.download(client, gpu_package["problem"]["id"], directory)
    assert (directory / "solution.cu").read_text() == source


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["problem"].update(language="python"),
        lambda p: p["problem"].update(function="solve"),
        lambda p: p["problem"].update(reference="private"),
        lambda p: p["problem"].update(wrong_solutions=["private"]),
        lambda p: p["problem"].update(operation="arbitrary-kernel"),
        lambda p: p["problem"].update(entrypoint="main"),
        lambda p: p["problem"]["comparison"].update(kind="numeric"),
        lambda p: p["problem"]["comparison"].update(absolute=1.0),
        lambda p: p["public_tests"][0].update(public=False),
        lambda p: p["public_tests"][0].update(public=True),
        lambda p: p["public_tests"][0].update(expected=[999]),
        lambda p: p["public_tests"][0].update(extra="hidden"),
        lambda p: p["public_tests"][0]["inputs"].update(b=[1, 2]),
        lambda p: p["public_tests"][0]["inputs"].update(a=[True]),
        lambda p: p["problem"]["examples"][0].update(public=False),
        lambda p: p["problem"]["examples"][0].update(public=1),
        lambda p: p.update(public_tests=[]),
        lambda p: p.update(public_tests=p["public_tests"] * 9),
    ],
)
def test_gpu_public_package_rejects_mixed_or_private_contracts(gpu_package, mutate):
    invalid = copy.deepcopy(gpu_package)
    mutate(invalid)
    with pytest.raises(practice.PracticeError, match="invalid public exercise package"):
        practice.validate_package(invalid)


def test_gpu_discovery_has_separate_runtime_and_resource_units(catalog, client, settings):
    version = version_fixture()
    settings.JUDGE_BACKEND = "wasi"
    settings.JUDGE_GPU_DEVICES = ()
    response = client.get("/practice/", {"language": "cuda"})
    assert [row["id"] for row in response.context["page"]] == [version.problem_id]
    card = response.context["page"][0]
    assert card["runtime"] == "cuda-gvisor" and card["library"] == "cuda-device"
    assert card["resource_kind"] == "gpu" and card["formal_units"] == 180
    assert card["supported"] is False
    text = response.content.decode()
    assert "180 秒 GPU 整卡时段" in text
    rows = text.split('<div class="problem-list">', 1)[1]
    assert "QuickJS" not in rows and "JavaScript 内置对象" not in rows
    assert not client.get("/practice/", {"language": "cuda", "supported": "1"}).context["page"]
    assert (
        client.get(
            "/practice/", {"runtime": "cuda-gvisor", "library": "cuda-device", "supported": "0"}
        ).context["page"][0]["id"]
        == version.problem_id
    )
    assert not client.get("/practice/", {"language": "cuda", "max_units": "999999"}).context["page"]
    assert not client.get("/practice/", {"max_gpu_seconds": "179"}).context["page"]
    response = client.get("/practice/", {"max_gpu_seconds": "180"})
    assert [row["id"] for row in response.context["page"]] == [version.problem_id]
    assert "max_gpu_seconds=180" in response.context["params"]
    assert not client.get("/practice/", {"max_gpu_seconds": "180", "max_units": "999999"}).context[
        "page"
    ]


@pytest.mark.parametrize("value", ["-1", "1e3", "１２３", "9" * 7, "NaN"])
def test_gpu_seconds_rejects_invalid_limits(client, gpu_package, value):
    assert not client.get("/practice/", {"max_gpu_seconds": value}).context["page"]


def test_gpu_compatibility_uses_current_verified_state(client, gpu_package, settings):
    device = make_device()
    settings.JUDGE_ENABLED = True
    settings.JUDGE_GPU_DEVICES = (device.pk,)
    assert client.get("/practice/", {"language": "cuda", "supported": "1"}).context["page"]
    device.checked_at = timezone.now() - timedelta(seconds=31)
    device.save(update_fields=["checked_at"])
    assert not client.get("/practice/", {"language": "cuda", "supported": "1"}).context["page"]


@pytest.mark.parametrize("mode", ["run", "submit"])
def test_gpu_remote_submission_keeps_source_version_and_request_identity(
    gpu_package, tmp_path, monkeypatch, mode
):
    client = practice.Client("https://example.test")
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: gpu_package)
    directory = tmp_path / "cuda-work"
    practice.download(client, gpu_package["problem"]["id"], directory)
    source = '// edited CUDA source; not executed\nextern "C" __global__ void solve() {}'
    (directory / "solution.cu").write_text(source)
    request = Mock(return_value={"status": "queued"})
    monkeypatch.setattr(client, "request", request)
    key = uuid.uuid4()
    assert practice.submit(client, directory, "opaque-test-token", key, mode) == {
        "status": "queued"
    }
    request.assert_called_once_with(
        f"/api/v1/problems/{gpu_package['problem']['id']}/submissions/",
        payload={"version_id": gpu_package["version_id"], "source": source, "mode": mode},
        token="opaque-test-token",
        key=key,
    )
