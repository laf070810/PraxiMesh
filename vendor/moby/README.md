# Moby seccomp profile

Unmodified `seccomp/default.json` and Apache-2.0 license from
[moby/profiles commit 6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef](https://github.com/moby/profiles/tree/6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef).

The profile SHA-256 is `6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6`.
PraxiMesh's experimental trusted runsc container adds seven namespace/filesystem
calls in `judge/container_runtime_service.py`. The original file is unchanged.
This configuration is for trusted infrastructure, not a learner sandbox.
