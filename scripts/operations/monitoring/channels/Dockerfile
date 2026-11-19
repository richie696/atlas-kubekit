# SPDX-License-Identifier: MIT
ARG PYTHON_BASE_IMAGE=python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
FROM ${PYTHON_BASE_IMAGE}

WORKDIR /opt/atlas
COPY --chown=10001:10001 scripts/lib/__init__.py scripts/lib/__init__.py
COPY --chown=10001:10001 scripts/lib/i18n.py scripts/lib/i18n.py
COPY --chown=10001:10001 scripts/operations/__init__.py scripts/operations/__init__.py
COPY --chown=10001:10001 scripts/operations/monitoring/__init__.py scripts/operations/monitoring/__init__.py
COPY --chown=10001:10001 scripts/operations/monitoring/channels/ scripts/operations/monitoring/channels/
COPY --chown=10001:10001 locales/ locales/

ENV PYTHONPATH=/opt/atlas
USER 10001:10001
ENTRYPOINT ["python", "-m", "scripts.operations.monitoring.channels.relay"]
