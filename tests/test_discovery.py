import pytest

from core.models import Problem
from core.resources import reservation_units

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "evaluator,slug",
    [
        ("python-ml-linear-v1", "train-linear-model"),
        ("python-weighted-fit-v1", "weighted-line-fit"),
        ("python-agent-tools-v1", "approved-ledger-report"),
    ],
)
def test_specialist_contract_is_selectable_and_does_not_leak_hidden_content(
    catalog, client, evaluator, slug
):
    response = client.get("/practice/", {"evaluator": evaluator})
    assert [card["id"] for card in response.context["page"]] == [slug]
    assert (evaluator, response.context["page"][0]["evaluator_label"]) in response.context[
        "evaluator_choices"
    ]
    version = Problem.objects.get(pk=slug).current
    card = response.context["page"][0]
    assert card["formal_units"] == reservation_units(version, "submit")
    assert "reference" not in card and "tests" not in card
    assert f'value="{evaluator}" selected' in response.content.decode()


def test_contract_options_follow_published_catalogue(catalog, client):
    Problem.objects.filter(id="approved-ledger-report").update(published=False)
    options = dict(client.get("/practice/").context["evaluator_choices"])
    assert "python-agent-tools-v1" not in options
    assert "python-ml-linear-v1" in options


def test_runtime_library_and_units_combine_without_using_worker_readiness(
    catalog, client, settings
):
    settings.JUDGE_BACKEND = "wasi"
    settings.JUDGE_ENABLED = False
    response = client.get(
        "/practice/",
        {"runtime": "python-wasi", "library": "python-stdlib", "max_units": "8", "supported": "1"},
    )
    assert response.context["page"].paginator.count > 0
    assert all(
        card["formal_units"] <= 8 and card["language"] == "python"
        for card in response.context["page"]
    )
    assert "max_units=8" in response.context["params"]
    assert not client.get(
        "/practice/", {"runtime": "python-wasi", "library": "javascript-builtins"}
    ).context["page"]


def test_gvisor_support_filter_keeps_readable_wasi_only_catalog(catalog, client, settings):
    settings.JUDGE_BACKEND = "gvisor"
    supported = client.get("/practice/", {"supported": "1"}).context["page"]
    assert all(card["evaluator"] == "python-function-v1" for card in supported)
    response = client.get("/practice/", {"supported": "0", "evaluator": "python-ml-linear-v1"})
    assert response.context["page"][0]["id"] == "train-linear-model"
    assert "仍可阅读与保存草稿" in response.content.decode()


@pytest.mark.parametrize(
    "parameters",
    [
        {"runtime": "cuda"},
        {"library": "torch"},
        {"supported": "yes"},
        {"max_units": "-1"},
        {"max_units": "NaN"},
        {"max_units": "1e2"},
        {"max_units": "９"},
        {"max_units": "9" * 10000},
        {"max_units": "0"},
    ],
)
def test_unknown_or_invalid_resource_filters_are_empty(catalog, client, parameters):
    assert not client.get("/practice/", parameters).context["page"]


def test_budget_filter_counts_hidden_cases_without_projecting_them(catalog, client):
    assert not client.get(
        "/practice/", {"evaluator": "python-agent-tools-v1", "max_units": "47"}
    ).context["page"]
    response = client.get("/practice/", {"evaluator": "python-agent-tools-v1", "max_units": "48"})
    assert response.context["page"][0]["formal_units"] == 48
