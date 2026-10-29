# Pass a verified digest-pinned official Python 3.11 image, see docs/operations.md.
ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock uv.lock ./
RUN python -m pip install --require-hashes -r requirements.lock
COPY requirements-judge.lock ./
ARG WASI_SHA256=""
ENV JUDGE_BACKEND=wasi JUDGE_WASI_SHA256=${WASI_SHA256}
COPY praximesh ./praximesh
COPY core ./core
COPY learning ./learning
COPY community ./community
COPY practice_api ./practice_api
COPY showcase ./showcase
COPY localization ./localization
COPY projects ./projects
COPY organizations ./organizations
COPY coaching ./coaching
COPY challenges ./challenges
COPY locale ./locale
COPY judge ./judge
COPY vendor/quickjs ./vendor/quickjs
COPY content ./content
COPY templates ./templates
COPY static ./static
COPY manage.py gunicorn.conf.py ./
COPY LICENSES.md ./
RUN python manage.py collectstatic --noinput && mkdir /data && chown 65532:65532 /data && chmod 700 /data
USER 65532:65532
EXPOSE 8000
CMD ["gunicorn", "praximesh.wsgi:application"]

# Worker uses the same code/locks as Web. It needs no Docker client or socket:
# learner Python still runs only inside WASI, under the reviewed driver limits.
FROM runtime AS worker
USER 0:0
RUN python -m pip install --require-hashes -r requirements-judge.lock
COPY runtime/wasi /opt/praximesh/wasi
ENV JUDGE_BACKEND=wasi JUDGE_WASI_BUNDLE=/opt/praximesh/wasi JUDGE_RECEIPT=/tmp/sandbox-receipt.json
USER 65532:65532
CMD ["python", "manage.py", "wasi_service"]

# Compose builds interpreters from the same pinned, verified sources as native deployment.
# Keep this stage independent of application edits so its expensive build is cached.
FROM ${PYTHON_IMAGE} AS wasi-build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 RAYON_NUM_THREADS=1 TOKIO_WORKER_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential pkg-config patch git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-judge.lock ./
RUN python -m pip install --require-hashes -r requirements-judge.lock
COPY scripts/build_wasi.py scripts/build_functions_wasi.py scripts/build_compose_wasi.py scripts/fetch_verified.py ./scripts/
COPY judge/__init__.py judge/wasi_policy.py judge/javascript_bridge.c ./judge/
COPY vendor ./vendor
RUN python scripts/build_compose_wasi.py

FROM runtime AS compose-web
COPY --from=wasi-build /build/wasi.sha256 /opt/praximesh/wasi.sha256
USER 0:0
RUN mkdir /ai && chown 65532:65532 /ai && chmod 700 /ai
USER 65532:65532
CMD ["python", "-m", "core.selfhost", "web"]

FROM compose-web AS compose-worker
USER 0:0
RUN python -m pip install --require-hashes -r requirements-judge.lock
COPY --from=wasi-build /build/wasi /opt/praximesh/wasi
USER 65532:65532
CMD ["python", "-m", "core.selfhost", "worker"]

# Keep the default/release target without WASI artifacts/runtime.
FROM runtime AS web
