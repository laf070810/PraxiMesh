FROM python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY source/requirements.lock /opt/praximesh/requirements.lock
RUN python -m pip install --require-hashes -r /opt/praximesh/requirements.lock
ARG CONTROLLER_CONTRACT
ARG WORKER_CONTRACT
ARG OBSERVER_CONTRACT
LABEL io.praximesh.controller.contract=${CONTROLLER_CONTRACT} io.praximesh.worker.contract=${WORKER_CONTRACT} io.praximesh.controller.observer=${OBSERVER_CONTRACT}
COPY docker /usr/local/bin/docker
COPY source /opt/praximesh/
COPY observer /opt/praximesh/nvml/
RUN mkdir -p /run/praximesh-engine /var/lib/praximesh-control && chmod 700 /run/praximesh-engine /var/lib/praximesh-control && printf '%s\n' "$CONTROLLER_CONTRACT" > /opt/praximesh/controller-contract && printf '%s\n' "$WORKER_CONTRACT" > /opt/praximesh/worker-contract
USER 0:0
WORKDIR /opt/praximesh
ENTRYPOINT ["/usr/local/bin/python", "-I", "/opt/praximesh/scripts/container_worker_entry.py"]
