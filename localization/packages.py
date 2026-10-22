"""Trusted translation imports, with no executable, private or structural overrides."""

import json
from pathlib import Path

from django.db import transaction

from core.content import ContentError, digest
from core.models import KnowledgeNode, ProblemVersion
from localization.models import KnowledgeSnapshot, KnowledgeTranslation, ProblemTranslation

NODE_TEXT = {"title", "summary", "body"}
NODE_LISTS = {"aliases", "outcomes"}
PROBLEM_TEXT = {"title", "summary", "statement", "constraints", "changelog"}
PROBLEM_LISTS = {"outcomes", "hints", "tags"}


def snapshot(node):
    key = digest(node.data)
    row, _ = KnowledgeSnapshot.objects.get_or_create(
        node=node, source_digest=key, defaults={"source_data": node.data}
    )
    if row.source_data != node.data:
        raise ContentError("Source snapshot does not match its digest.")
    return row


def load_translations(path):
    path = Path(path)
    if path.stat().st_size > 5_000_000:
        raise ContentError("Translation package exceeds 5 MB.")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContentError("Duplicate translation key.")
            result[key] = value
        return result

    def invalid(value):
        raise ContentError("Nonfinite translation value.")

    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=invalid
    )


def exact_keys(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - set(optional)
    ):
        raise ContentError("Invalid translation fields.")


def translated_fields(value, source, *, problem=False):
    text = PROBLEM_TEXT if problem else NODE_TEXT
    lists = PROBLEM_LISTS if problem else NODE_LISTS
    required = text | lists | ({"examples"} if problem else set())
    exact_keys(value, required)
    for field in text:
        if (
            not isinstance(value[field], str)
            or len(value[field]) > 50000
            or (source[field] and not value[field].strip())
        ):
            raise ContentError("Invalid translated text.")
    for field in lists:
        items = value[field]
        if (
            not isinstance(items, list)
            or len(items) > 100
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 5000 for item in items
            )
        ):
            raise ContentError("Invalid translated text list.")
        if field not in {"aliases", "tags"} and len(items) != len(source[field]):
            raise ContentError("Translation must retain every source learning objective and hint.")
    if problem:
        examples = value["examples"]
        # Positions refer only to the public projection of this exact immutable version.
        public = [case for case in source["tests"] if case["public"]]
        if not isinstance(examples, list) or len(examples) != len(public):
            raise ContentError("Translation must name each public example.")
        if any(
            not isinstance(name, str) or not name.strip() or len(name) > 200 for name in examples
        ):
            raise ContentError("Invalid public example name.")
    return value


@transaction.atomic
def import_translations(package):
    exact_keys(package, {"schema_version", "locale", "nodes", "problems"})
    if package["schema_version"] != 1 or package["locale"] != "en":
        raise ContentError("Unsupported translation package or locale.")
    counts = {"nodes": 0, "problems": 0}
    for kind in counts:
        entries = package[kind]
        if not isinstance(entries, list) or len(entries) > 1000:
            raise ContentError("Invalid translation entry list.")
        seen = set()
        for entry in entries:
            required = {"id", "source_digest", "revision", "fields"}
            if kind == "problems":
                required.add("source_revision")
            exact_keys(entry, required, {"published"})
            if (
                not isinstance(entry["id"], str)
                or not isinstance(entry["source_digest"], str)
                or type(entry["revision"]) is not int
                or entry["revision"] < 1
                or type(entry.get("published", True)) is not bool
            ):
                raise ContentError("Invalid translation identity.")
            identity = (entry["id"], entry["source_digest"])
            if identity in seen:
                raise ContentError("Duplicate translation source.")
            seen.add(identity)
            if kind == "nodes":
                # Lock the stable owner; new translations cannot fabricate source snapshots.
                node = KnowledgeNode.objects.select_for_update().filter(pk=entry["id"]).first()
                source = (
                    KnowledgeSnapshot.objects.filter(
                        node=node, source_digest=entry["source_digest"]
                    ).first()
                    if node
                    else None
                )
                if source is None:
                    raise ContentError("Unknown knowledge source version or digest.")
                model, target, data = KnowledgeTranslation, {"snapshot": source}, source.source_data
            else:
                if type(entry["source_revision"]) is not int:
                    raise ContentError("Invalid source revision.")
                source = (
                    ProblemVersion.objects.select_for_update()
                    .filter(
                        problem_id=entry["id"],
                        revision=entry["source_revision"],
                        digest=entry["source_digest"],
                    )
                    .first()
                )
                if source is None:
                    raise ContentError("Unknown problem source version or digest.")
                model, target, data = ProblemTranslation, {"version": source}, source.data
            fields = translated_fields(entry["fields"], data, problem=kind == "problems")
            checksum = digest(fields)
            rows = model.objects.filter(**target, locale=package["locale"])
            if rows.filter(revision__gt=entry["revision"]).exists():
                raise ContentError("Cannot silently roll back a translation revision.")
            row, created = rows.get_or_create(
                revision=entry["revision"],
                defaults={
                    **target,
                    "locale": package["locale"],
                    "translation_digest": checksum,
                    "fields": fields,
                    "published": entry.get("published", True),
                },
            )
            if not created:
                if row.translation_digest != checksum or row.fields != fields:
                    raise ContentError("Immutable translation revision changed.")
                row.published = entry.get("published", True)
                row.save(update_fields=["published"])
            counts[kind] += 1
    return counts
