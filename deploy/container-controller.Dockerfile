# Trusted CPU control development endpoint; no product worker/admission.
FROM python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15
ARG CONTROLLER_CONTRACT
LABEL io.praximesh.controller.contract=${CONTROLLER_CONTRACT}
COPY docker /usr/local/bin/docker
COPY source /opt/praximesh/
RUN mkdir -p /run/praximesh-engine /var/lib/praximesh-control && chmod 700 /run/praximesh-engine /var/lib/praximesh-control && printf '%s\n' "$CONTROLLER_CONTRACT" > /opt/praximesh/controller-contract
USER 0:0
WORKDIR /opt/praximesh
ENTRYPOINT ["/usr/local/bin/python", "-I", "/opt/praximesh/scripts/container_controller_fixture.py"]
