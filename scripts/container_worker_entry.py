"""Fixed image entrypoint. No arbitrary management command or configuration path."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from judge.container_worker_image import verify_image

    try:
        if sys.argv[1:] not in ([], ["--check"], ["--once"]):
            raise ValueError("Expected no arguments, --check or --once")
        verify_image()
        os.environ["DJANGO_SETTINGS_MODULE"] = "praximesh.container_worker_settings"
        import django
        from django.core.management import call_command
        from django.core.management.base import CommandError

        django.setup()
        try:
            call_command(
                "container_gpu_worker", check="--check" in sys.argv, once="--once" in sys.argv
            )
        except CommandError as error:
            # This command emits only a fixed stage and exception class.
            print(str(error), file=sys.stderr)
            return 1
        return 0
    except Exception as error:
        # Avoid Django's default traceback for setup/database errors too.
        print("Container GPU worker stopped: " + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
