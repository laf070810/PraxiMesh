from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0004_gpu_resources")]

    operations = [
        migrations.AddField(
            model_name="gpudevice",
            name="environment",
            field=models.JSONField(default=dict),
        ),
    ]
