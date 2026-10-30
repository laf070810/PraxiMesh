ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15
FROM ${PYTHON_IMAGE} AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake ca-certificates && rm -rf /var/lib/apt/lists/*
COPY scripts/build_compose_tutor.py /build_compose_tutor.py
COPY scripts/fetch_verified.py /scripts/fetch_verified.py
RUN python /build_compose_tutor.py

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=2
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/out/bin/llama-server /usr/local/bin/llama-server
COPY --from=build --chmod=0444 /build/tutor.gguf /opt/tutor.gguf
COPY --from=build --chmod=0444 /build/LICENSE.qwen /opt/LICENSE.qwen
COPY --from=build /build/licenses /opt/llama-licenses
COPY --from=build /build/llama.cpp-620d2225cc436ab0b001e3200e590e3b286ef9ab/LICENSE /opt/LICENSE.llama.cpp
COPY scripts/compose_tutor.py /compose_tutor.py
USER 65532:65532
CMD ["python", "/compose_tutor.py"]
