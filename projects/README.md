# Owner-published multi-stage projects

Register `projects` in Django and include `projects.urls` under `/projects/`.
The public routes are `projects:index`, `projects:detail(slug)`,
`projects:revision(slug, number)` and `projects:stage(slug, number, key)`.
All HTTP routes are read-only. Authenticated pages add only the requesting
learner's private formal result evidence.

Publish a reviewed problem catalog first. With **owner database credentials**:

```sh
python manage.py project_import /path/to/plans.json --check
python manage.py project_import /path/to/plans.json
python manage.py project_withdraw sensor-analysis
```

The import is atomic and only publishes IDs present in the supplied file. An
omitted project is unchanged. A same-revision import must match the complete
immutable plan and its stored stage bindings. Older project revisions cannot
replace a newer current revision. Each stage pins a stable problem ID, revision
number and content digest; it stores a real `ProblemVersion` foreign key.

A plan contains 2–20 distinct tasks, with exactly one final integration task
ordered last. Recommendations refer to earlier stages, form an acyclic graph,
and connect every component to the final task. They do not gate navigation or
submission. Content review and actual sandbox tests must establish that the
final task truly integrates the components; metadata alone cannot establish it.

Grant Web **SELECT only** on `projects_project`, `projects_projectrevision`,
and `projects_projectstage`; grant worker no access. The database owner alone
publishes or withdraws plans. There is no HTTP publication endpoint or new
per-user storage to export/delete. Existing account exports and deletion of
private submissions already cover the evidence from which progress is derived.

Progress means a finished formal accepted submission for that exact pinned
version with valid accepted counts; it does not mean mastery. Run, pending,
platform-error, malformed results, other users and other versions do not count.
Upgrading a current problem does not erase an old project's exact-version
evidence. Archived stages display their original public statement and samples
but cannot accept old-version submissions through the current core workflow;
current-version practice is clearly separate. Publishing a new project revision
is the explicit way to update pins. Withdrawing a problem removes its stage
content and practice links while retaining the learner's old private evidence.
