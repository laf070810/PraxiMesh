"""Read-only text projections; project plans and progress evidence stay unchanged."""

from localization.presentation import localized_problem
from projects.progress import stage_cards


def localized_stage_cards(revision, user):
    cards = stage_cards(revision, user)
    versions = {
        stage.key: stage.problem_version
        for stage in revision.stages.select_related("problem_version")
    }
    for card in cards:
        # Withdrawn problems have no title projection or accessible stage page.
        if card["available"]:
            problem = localized_problem(versions[card["key"]])
            card["problem_title"] = problem["title"]
            card["problem_language"] = problem["content_language"]
            card["problem_translation_fallback"] = problem["translation_fallback"]
    return cards
