from django.db import migrations


def initialize(apps, schema_editor):
    apps.get_model("coaching", "SlotLock").objects.using(
        schema_editor.connection.alias
    ).get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("coaching", "0001_initial")]
    operations = [migrations.RunPython(initialize, migrations.RunPython.noop)]
