"""Prepare only public activity definitions; all learner actions use real browsers."""

import runpy
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_journey(environment, owner_run, artifacts):
    slug = "acceptance-challenge-" + uuid.uuid4().hex[:12]
    env = dict(environment, BROWSER_CHALLENGE=slug)
    owner_run(
        "manage.py",
        "shell",
        "-c",
        "import os\nfrom datetime import timedelta\nfrom django.utils import timezone\n"
        "from challenges.catalog import import_package\nfrom core.models import Problem\n"
        "tasks=[]\n"
        "for slug in ('sum-positive','population-stats'):\n"
        "    version=Problem.objects.get(pk=slug).current\n"
        "    tasks.append(dict(id=slug,title=version.data['title'],description='Public acceptance task',"
        "problem=slug,problem_revision=version.revision,problem_digest=version.digest))\n"
        "now=timezone.now()\n"
        "import_package(dict(schema_version=1,challenges=[dict(id=os.environ['BROWSER_CHALLENGE'],"
        "revision=1,title='Browser practice challenge',summary='Public synthetic activity.',"
        "rules='Private progress; formal passes after joining within this window.',"
        "starts_at=(now-timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),"
        "ends_at=(now+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),tasks=tasks)]))\n",
        env=env,
    )
    previous_path = sys.path[:]
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        module = runpy.run_path(str(ROOT / "tests/browser_challenges.py"))
        module["run_journey"](env, artifacts)
    finally:
        sys.path[:] = previous_path
        owner_run(
            "manage.py",
            "shell",
            "-c",
            "import os\nfrom challenges.catalog import withdraw\nfrom challenges.models import Challenge\n"
            "item=Challenge.objects.select_related('current').get(pk=os.environ['BROWSER_CHALLENGE'])\n"
            "withdraw(item.pk,item.current.digest)\n",
            env=env,
        )
