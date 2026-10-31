# Experimental trusted runtime; no GPU devices or product admission.
FROM python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15 AS guest
FROM python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15
ARG RUNTIME_CONTRACT
LABEL io.praximesh.supervision.protocol="praximesh-container-supervision-v1"
LABEL io.praximesh.supervision.runtime=${RUNTIME_CONTRACT}
COPY --from=guest / /opt/guest-root/
COPY gpu_runtime_cpu_guest.py /opt/guest-root/opt/praximesh/cpu_guest.py
COPY runtime /opt/runsc/
COPY gvisor-LICENSE /opt/licenses/gvisor-LICENSE
COPY judge /opt/praximesh/judge/
COPY container_entry.py container_contract.py container_runtime_fixture.py container_transport_fixture.py /opt/praximesh/
RUN chmod -R a+rX /opt/runsc && mkdir -p /run/praximesh && chown 65532:65532 /run/praximesh && chmod 700 /run/praximesh
USER 65532:65532
WORKDIR /run/praximesh
ENTRYPOINT ["/usr/local/bin/python", "-I", "/opt/praximesh/container_entry.py"]
