"""Explainable practice summaries, never a mastery score or an inferred diagnosis."""

from collections import Counter, defaultdict
from datetime import timedelta

from django.db.models import Count, Max, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext

from core.models import KnowledgeNode, Problem, Submission
from localization.presentation import DIFFICULTIES, localized_node, localized_problem
from localization.presentation import VERDICTS as LABELS

LEARNER_VERDICTS = tuple(key for key in LABELS if key != "internal_error")
RECENT_DAYS = 30


def build_evidence(user, *, now=None):
    now = now or timezone.now()
    since = now - timedelta(days=RECENT_DAYS)
    nodes = {node.pk: node for node in KnowledgeNode.objects.filter(published=True)}
    concepts = {key: node for key, node in nodes.items() if node.kind == "concept"}
    problems = list(
        Problem.objects.filter(published=True, current__isnull=False).select_related("current")
    )
    current_ids = {problem.current_id for problem in problems}
    jobs = Submission.objects.filter(
        user=user, mode="submit", status="finished", verdict__in=LEARNER_VERDICTS
    ).annotate(result_at=Coalesce("finished_at", "created_at"))
    # Aggregate in the DB instead of loading private source/result payloads or every job.
    results = defaultdict(list)
    for row in (
        jobs.order_by()
        .values("version_id", "verdict")
        .annotate(
            count=Count("pk"),
            recent=Count("pk", filter=Q(result_at__gte=since)),
            latest=Max("result_at"),
        )
    ):
        results[row["version_id"]].append(row)

    cards = []
    for problem in problems:
        version = problem.current
        rows = results[version.pk]
        errors = {
            row["verdict"]: row["recent"]
            for row in rows
            if row["verdict"] != "accepted" and row["recent"]
        }
        latest = max(rows, key=lambda row: row["latest"], default=None)
        cards.append(
            {
                "id": problem.pk,
                "title": localized_problem(version)["title"],
                "translation_fallback": localized_problem(version)["translation_fallback"],
                "revision": version.revision,
                "difficulty": version.data["difficulty"],
                "concepts": set(version.data["concepts"]) & concepts.keys(),
                "attempts": sum(row["count"] for row in rows),
                "accepted_attempts": sum(
                    row["count"] for row in rows if row["verdict"] == "accepted"
                ),
                "recent_errors": errors,
                "last_result_at": latest["latest"] if latest else None,
                "last_verdict": latest["verdict"] if latest else None,
            }
        )
    for card in cards:
        card["completed"] = card["accepted_attempts"] > 0

    def summarize(items):
        errors = Counter()
        for item in items:
            errors.update(item["recent_errors"])
        return {
            "problems": len(items),
            "attempted": sum(item["attempts"] > 0 for item in items),
            "completed": sum(item["completed"] for item in items),
            "attempts": sum(item["attempts"] for item in items),
            "accepted_attempts": sum(item["accepted_attempts"] for item in items),
            "recent_errors": [
                {"verdict": verdict, "label": LABELS[verdict], "count": count}
                for verdict, count in sorted(errors.items())
            ],
            "difficulty_evidence": [
                {"difficulty": DIFFICULTIES.get(difficulty, difficulty), "count": count}
                for difficulty, count in sorted(
                    Counter(item["difficulty"] for item in items if item["completed"]).items()
                )
            ],
        }

    concept_rows = [
        {
            "id": key,
            "title": localized_node(node)["title"],
            "translation_fallback": localized_node(node)["translation_fallback"],
            **summarize([card for card in cards if key in card["concepts"]]),
        }
        for key, node in concepts.items()
    ]
    domain_rows = []
    for key, node in nodes.items():
        if node.kind != "domain":
            continue
        # A multi-concept problem counts once within a domain, even when several
        # of its concepts share that domain. Cross-domain membership is explicit.
        domain_concepts = {
            concept_id
            for concept_id, concept in concepts.items()
            if key in concept.data["domains"]
            or any(
                key in nodes[topic].data["domains"]
                for topic in concept.data["topics"]
                if topic in nodes
            )
        }
        domain_rows.append(
            {
                "id": key,
                "title": localized_node(node)["title"],
                "translation_fallback": localized_node(node)["translation_fallback"],
                **summarize([card for card in cards if card["concepts"] & domain_concepts]),
            }
        )

    evidenced_concepts = set().union(*(card["concepts"] for card in cards if card["completed"]))
    recommendations = []
    recommended = set()

    def recommend(card, kind, reason):
        if card["id"] not in recommended:
            recommended.add(card["id"])
            recommendations.append({"problem": card, "kind": kind, "reason": reason})

    # Recent concrete failures precede generic suggestions, even if a problem
    # also has an older AC. This recommends practice without claiming weakness.
    for card in cards:
        if card["recent_errors"] and card["last_verdict"] != "accepted":
            recommend(
                card,
                gettext("复习"),
                gettext("当前版本最近一次正式结果为「%(verdict)s」，可复查边界与约束。")
                % {"verdict": LABELS[card["last_verdict"]]},
            )
    for card in cards:
        if card["completed"]:
            continue
        for concept_id in sorted(card["concepts"]):
            for edge in concepts[concept_id].data["relations"]:
                target = edge["target"]
                if edge["kind"] not in {"required", "recommended"} or target in evidenced_concepts:
                    continue
                if target not in concepts:
                    continue
                prerequisite = next(
                    (
                        candidate
                        for candidate in cards
                        if candidate["id"] != card["id"]
                        and target in candidate["concepts"]
                        and not candidate["completed"]
                    ),
                    None,
                )
                if prerequisite:
                    recommend(
                        prerequisite,
                        gettext("前置练习"),
                        gettext(
                            "「%(problem)s」涉及「%(concept)s」；其前置「%(target)s」尚无当前题正式通过记录。可先练习，也可以跳过。"
                        )
                        % {
                            "problem": card["title"],
                            "concept": localized_node(concepts[concept_id])["title"],
                            "target": localized_node(concepts[target])["title"],
                        },
                    )
        if card["attempts"]:
            reason = gettext("当前版本已有 %(attempts)s 次正式结果，尚无通过记录。") % {
                "attempts": card["attempts"]
            }
        else:
            reason = gettext("当前版本尚无正式评测记录，可以从公开样例开始。")
        recommend(card, gettext("继续练习"), reason)
    for card in cards:
        if card["completed"] and card["last_result_at"] < since:
            recommend(
                card,
                gettext("间隔复习"),
                gettext("当前版本已有通过记录，最近正式结果距今超过 %(days)s 天；可尝试独立重做。")
                % {"days": RECENT_DAYS},
            )

    recent_errors = [
        {
            "id": job.pk,
            "problem": localized_problem(job.version)["title"],
            "revision": job.version.revision,
            "label": LABELS[job.verdict],
            "result_at": job.result_at,
        }
        for job in (
            jobs.filter(version_id__in=current_ids, result_at__gte=since)
            .exclude(verdict="accepted")
            .select_related("version")
            .defer("source", "result", "environment")
            .order_by("-result_at", "-pk")[:10]
        )
    ]
    return {
        "summary": summarize(cards),
        "domains": domain_rows,
        "concepts": concept_rows,
        "recommendations": recommendations,
        "recent_errors": recent_errors,
        "recent_days": RECENT_DAYS,
        "archived_attempts": sum(
            row["count"]
            for version_id, rows in results.items()
            if version_id not in current_ids
            for row in rows
        ),
    }
