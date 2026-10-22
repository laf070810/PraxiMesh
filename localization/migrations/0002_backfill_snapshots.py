"""Retain only source versions actually present at upgrade; no invented history."""

import hashlib
import json

from django.db import migrations


def backfill(apps, schema_editor):
    Node = apps.get_model("core", "KnowledgeNode")
    Snapshot = apps.get_model("localization", "KnowledgeSnapshot")
    for node in Node.objects.using(schema_editor.connection.alias).iterator():
        checksum = hashlib.sha256(
            json.dumps(node.data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
        Snapshot.objects.using(schema_editor.connection.alias).get_or_create(
            node_id=node.pk,
            source_digest=checksum,
            defaults={"source_data": node.data, "source_locale": "zh-hans"},
        )


class Migration(migrations.Migration):
    dependencies = [("localization", "0001_initial")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
