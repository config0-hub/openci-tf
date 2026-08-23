# Use the official AWS Lambda Python 3.14 base image
FROM public.ecr.aws/lambda/python:3.14

# EXTRA_CA_CERT defaults to an empty tracked placeholder. Developers behind a TLS
# interception proxy can override it with a local, ignored certificate file.
ARG EXTRA_CA_CERT=docker/certs/extra-ca.crt.optional
COPY ${EXTRA_CA_CERT} /tmp/extra-ca.crt
RUN if [ -s /tmp/extra-ca.crt ]; then cp /tmp/extra-ca.crt /etc/pki/ca-trust/source/anchors/extra-ca.crt && update-ca-trust extract; fi
ENV REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    PIP_CERT=/etc/pki/tls/certs/ca-bundle.crt

# Install git and the pinned sops binary used by PrepareAndSubmit.  The release
# checksum is verified before it is put on PATH.
ARG SOPS_VERSION=3.9.4
ARG SOPS_SHA256=5488e32bc471de7982ad895dd054bbab3ab91c417a118426134551e9626e4e85
# curl is provided by curl-minimal in the base image; installing "curl" conflicts.
RUN dnf install -y git tar && dnf clean all \
    && curl --fail --location --retry 3 -o /usr/local/bin/sops "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
    && echo "${SOPS_SHA256}  /usr/local/bin/sops" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/sops

# Copy requirements file and install dependencies
COPY requirements.txt ./
RUN python3 -m pip install -r requirements.txt --target ${LAMBDA_TASK_ROOT}

# Copy function code
COPY src/ ${LAMBDA_TASK_ROOT}/src/

# Default handler — overridden per-Lambda via image_config.command
CMD ["src.services.webhook.handler.handler"]
