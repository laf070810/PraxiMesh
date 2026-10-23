"""Private facts derived from verified formal results, never certificates or mastery scores."""

from collections import defaultdict

from django.utils.translation import gettext_lazy as _

from challenges.models import Participation
from challenges.progress import task_cards
from core.result_evidence import evidence_jobs, is_formal_evidence
from projects.models import ProjectStage

RULES = (
    (
        "first-pass-v1",
        _("首次正式通过"),
        _("至少一道可访问题目有可信正式通过记录。"),
        "problems",
        1,
    ),
    (
        "five-problems-v1",
        _("五道不同题目"),
        _("五道不同题目正式通过；同一题的不同版本与重复提交只计一次。"),
        "problems",
        5,
    ),
    (
        "three-contracts-v1",
        _("尝试三类评测"),
        _("至少三种评测契约有可信正式通过记录。"),
        "contracts",
        3,
    ),
    (
        "project-complete-v1",
        _("完成固定版本项目"),
        _("同一已发布项目修订的全部可访问阶段都有正式通过记录。"),
        "projects",
        1,
    ),
    (
        "challenge-complete-v1",
        _("完成一次挑战"),
        _("本人已加入的活动修订中，全部任务在规定时间内正式通过。"),
        "challenges",
        1,
    ),
)


def achievements(user):
    versions, problems, contracts = set(), set(), set()
    records = evidence_jobs(user).filter(
        verdict="accepted",
        version__problem__published=True,
        version__problem__current__isnull=False,
    )
    for record in records.iterator(chunk_size=100):
        if is_formal_evidence(record):
            versions.add(record.version_id)
            problems.add(record.version.problem_id)
            contracts.add(record.version.data["evaluator"])
    stages = defaultdict(list)
    projects = {}
    for project, revision, version in (
        ProjectStage.objects.filter(
            revision__project__published=True, revision__project__current__isnull=False
        )
        .values_list("revision__project_id", "revision_id", "problem_version_id")
        .iterator(chunk_size=100)
    ):
        projects[revision] = project
        stages[revision].append(version)
    completed_projects = {
        projects[revision] for revision, pins in stages.items() if set(pins) <= versions
    }
    completed_challenges = set()
    for row in (
        Participation.objects.filter(
            user=user,
            revision__challenge__published=True,
            revision__challenge__current__isnull=False,
        )
        .select_related("revision")
        .iterator(chunk_size=100)
    ):
        cards = task_cards(row.revision, row)
        if cards and all(card["available"] and card["evidence"] is not None for card in cards):
            completed_challenges.add(row.revision.challenge_id)
    counts = {
        "problems": len(problems),
        "contracts": len(contracts),
        "projects": len(completed_projects),
        "challenges": len(completed_challenges),
    }
    return [
        {
            "id": key,
            "title": title,
            "rule": rule,
            "count": counts[metric],
            "target": target,
            "achieved": counts[metric] >= target,
        }
        for key, title, rule, metric, target in RULES
    ]


def export_user_data(user):
    return [
        {key: row[key] for key in ("id", "count", "target", "achieved")}
        for row in achievements(user)
    ]
