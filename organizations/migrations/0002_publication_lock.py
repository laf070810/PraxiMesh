from django.db import migrations


def initialize(apps, schema_editor):
    apps.get_model("organizations", "PublicationLock").objects.create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("organizations", "0001_initial")]
    operations = [migrations.RunPython(initialize, migrations.RunPython.noop)]
