"""Bounded, read-only multidimensional evidence from the trusted result projection."""

from collections import Counter
from datetime import timedelta

from django.utils import timezone

from core.models import KnowledgeNode, Submission
from core.result_evidence import InvalidResult, verified_evidence
from localization.presentation import VERDICTS, localized_node, localized_problem

MAX_RECORDS = 500


def build(user, *, now=None):
    now = now or timezone.now()
    nodes = {n.pk: n for n in KnowledgeNode.objects.filter(published=True)}
    jobs = list(
        Submission.objects.filter(
            user=user,
            mode="submit",
            status="finished",
            version__problem__published=True,
            version__problem__current__isnull=False,
        )
        .select_related("version__problem")
        .defer("source")
        .order_by("-finished_at", "-created_at", "-id")[: MAX_RECORDS + 1]
    )
    versions = {}
    rejected = 0
    for job in jobs[:MAX_RECORDS]:
        try:
            metrics = verified_evidence(job)
        except (InvalidResult, TypeError, ValueError):
            rejected += 1
            continue
        row = versions.setdefault(
            job.version_id,
            {
                "version": job.version,
                "latest_id": job.pk,
                "latest_at": job.finished_at,
                "latest_verdict": metrics["verdict"],
                "latest_label": VERDICTS[metrics["verdict"]],
                "accepted": False,
                "formal_results": 0,
            },
        )
        row["accepted"] |= metrics["verdict"] == "accepted"
        row["formal_results"] += 1
    cards = []
    for row in versions.values():
        version = row.pop("version")
        public = localized_problem(version)
        cards.append(
            {
                **row,
                "version_id": version.pk,
                "problem_id": version.problem_id,
                "problem": public,
                "revision": version.revision,
                "archived": version.problem.current_id != version.pk,
                "concepts": set(version.data["concepts"]) & nodes.keys(),
                "difficulty": public["difficulty_label"],
                "kind": version.data["evaluator"],
                "stale": row["latest_at"] < now - timedelta(days=30),
                "failure_after_accept": row["accepted"] and row["latest_verdict"] != "accepted",
            }
        )

    def summarize(group):
        return {
            "distinct_problems": len({c["problem_id"] for c in group}),
            "versions": len(group),
            "accepted_problems": len({c["problem_id"] for c in group if c["accepted"]}),
            "difficulties": sorted(Counter(c["difficulty"] for c in group).items()),
            "kinds": sorted(Counter(c["kind"] for c in group).items()),
            "recent_failure_versions": sum(c["failure_after_accept"] for c in group),
            "stale_versions": sum(c["stale"] for c in group),
        }

    concepts = []
    domains = []
    for node in nodes.values():
        if node.kind == "concept":
            group = [c for c in cards if node.pk in c["concepts"]]
            concepts.append({"id": node.pk, "node": localized_node(node), **summarize(group)})
        elif node.kind == "domain":
            members = {
                c.pk
                for c in nodes.values()
                if c.kind == "concept"
                and (
                    node.pk in c.data["domains"]
                    or any(
                        node.pk in nodes[t].data["domains"] for t in c.data["topics"] if t in nodes
                    )
                )
            }
            domains.append(
                {
                    "id": node.pk,
                    "node": localized_node(node),
                    **summarize([c for c in cards if c["concepts"] & members]),
                }
            )
    from coaching.models import LearningPlan

    active = LearningPlan.objects.filter(user=user, active=True).first()
    goal_ids = {row["id"] for row in active.targets} if active else set()
    return {
        "goals": [row for row in concepts if row["id"] in goal_ids],
        "cards": cards,
        "concepts": concepts,
        "domains": domains,
        "summary": summarize(cards),
        "limited": len(jobs) > MAX_RECORDS,
        "examined": min(len(jobs), MAX_RECORDS),
        "excluded": rejected,
        "record_limit": MAX_RECORDS,
    }
