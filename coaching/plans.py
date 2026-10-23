"""Learner-controlled, version-pinned rule suggestions; never a model inference."""

from copy import copy

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404

from coaching import evidence
from coaching.models import LearningPlan
from core.content import digest
from core.models import KnowledgeNode, Problem, ProblemVersion
from localization.models import KnowledgeSnapshot
from localization.presentation import localized_node, localized_problem


class PlanError(ValueError):
    pass


@transaction.atomic
def create(user, target_ids):
    if not user.is_authenticated:
        raise Http404
    if (
        not isinstance(target_ids, list)
        or not 1 <= len(target_ids) <= 8
        or len(set(target_ids)) != len(target_ids)
    ):
        raise PlanError("invalid_targets")
    get_user_model().objects.select_for_update().get(pk=user.pk)
    nodes = {n.pk: n for n in KnowledgeNode.objects.filter(published=True, kind="concept")}
    if not set(target_ids).issubset(nodes):
        raise PlanError("invalid_targets")
    selected = set(target_ids)
    order = []
    visited = set()

    def prerequisites(key):
        if key in visited:
            return
        visited.add(key)
        for edge in nodes[key].data["relations"]:
            if edge["kind"] in {"required", "recommended"} and edge["target"] in nodes:
                prerequisites(edge["target"])
        order.append(key)

    for key in target_ids:
        prerequisites(key)
    ranks = {key: i for i, key in enumerate(order)}
    previous = {c["version_id"]: c for c in evidence.build(user)["cards"]}
    candidates = []
    for problem in Problem.objects.filter(published=True, current__isnull=False).select_related(
        "current"
    ):
        bindings = set(problem.current.data["concepts"]) & visited
        if not bindings:
            continue
        row = previous.get(problem.current_id)
        reason = "prerequisite" if not bindings & selected else "goal"
        if row:
            if row["latest_verdict"] != "accepted":
                reason = "recent_result"
            elif row["stale"]:
                reason = "spaced_review"
            elif row["accepted"]:
                continue
        candidates.append(
            (
                min(ranks[key] for key in bindings),
                problem.pk,
                {
                    "version_id": problem.current_id,
                    "digest": problem.current.digest,
                    "concepts": sorted(bindings),
                    "reason": reason,
                },
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    LearningPlan.objects.filter(user=user, active=True).update(active=False)
    return LearningPlan.objects.create(
        user=user,
        targets=[{"id": key, "digest": digest(nodes[key].data)} for key in target_ids],
        suggestions=[row[2] for row in candidates[:24]],
    )


@transaction.atomic
def action(user, pk, operation, version_id=None):
    plan = LearningPlan.objects.select_for_update().filter(pk=pk, user=user).first()
    if plan is None:
        raise Http404
    if operation == "delete":
        plan.delete()
    elif operation == "close":
        plan.active = False
        plan.save(update_fields=["active"])
    elif operation in {"skip", "restore"}:
        if not plan.active or version_id not in [s["version_id"] for s in plan.suggestions]:
            raise PlanError("invalid_suggestion")
        skipped = set(plan.skipped)
        skipped.add(version_id) if operation == "skip" else skipped.discard(version_id)
        plan.skipped = sorted(skipped)
        plan.save(update_fields=["skipped"])
    else:
        raise PlanError("invalid_action")


def target_projection(node, source_digest):
    if node is None:
        return None
    if digest(node.data) != source_digest:
        snapshot = KnowledgeSnapshot.objects.filter(node=node, source_digest=source_digest).first()
        if snapshot is None or digest(snapshot.source_data) != source_digest:
            return None
        node = copy(node)
        node.data = snapshot.source_data
    return localized_node(node)


def present(plan):
    nodes = {
        n.pk: n
        for n in KnowledgeNode.objects.filter(
            pk__in=[t["id"] for t in plan.targets], published=True
        )
    }
    targets = [
        {
            "id": row["id"],
            "digest": row["digest"],
            "node": target_projection(nodes.get(row["id"]), row["digest"]),
            "changed": row["id"] in nodes and digest(nodes[row["id"]].data) != row["digest"],
        }
        for row in plan.targets
    ]
    versions = {
        v.pk: v
        for v in ProblemVersion.objects.filter(
            pk__in=[s["version_id"] for s in plan.suggestions],
            problem__published=True,
            problem__current__isnull=False,
        ).select_related("problem")
    }
    suggestions = []
    for row in plan.suggestions:
        version = versions.get(row["version_id"])
        visible = version is not None and version.digest == row["digest"]
        suggestions.append(
            {
                **row,
                "skipped": row["version_id"] in plan.skipped,
                "problem": localized_problem(version) if visible else None,
                "archived": visible and version.problem.current_id != version.pk,
            }
        )
    return {"plan": plan, "targets": targets, "suggestions": suggestions}
