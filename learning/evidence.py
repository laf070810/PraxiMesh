"""Explainable practice summaries, never a mastery score or an inferred diagnosis."""

from collections import Counter, defaultdict
from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext

from core.models import KnowledgeNode, Problem
from core.result_evidence import evidence_jobs, is_formal_evidence
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
    # Bound memory to version/verdict aggregates and ten recent errors. The
    # stored source is deferred, and each result is checked before it counts.
    aggregate = {}
    recent_errors = []
    for record in evidence_jobs(user).iterator(chunk_size=100):
        if not is_formal_evidence(record):
            continue
        key = (record.version_id, record.verdict)
        row = aggregate.setdefault(
            key,
            {
                "version_id": record.version_id,
                "verdict": record.verdict,
                "count": 0,
                "recent": 0,
                "latest": record.finished_at,
            },
        )
        row["count"] += 1
        row["recent"] += record.finished_at >= since
        if (
            len(recent_errors) < 10
            and record.version_id in current_ids
            and record.finished_at >= since
            and record.verdict != "accepted"
        ):
            recent_errors.append(
                {
                    "id": record.pk,
                    "problem": localized_problem(record.version)["title"],
                    "revision": record.version.revision,
                    "label": LABELS[record.verdict],
                    "result_at": record.finished_at,
                }
            )
    results = defaultdict(list)
    for row in aggregate.values():
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
