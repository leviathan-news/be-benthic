FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0

COPY requirements.lock /opt/reader-requirements.lock
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/reader-venv \
    && /opt/reader-venv/bin/pip install --no-cache-dir --require-hashes -r /opt/reader-requirements.lock
COPY worker.py /opt/reader/worker.py
COPY extract.js /opt/reader/extract.js
USER 1000:1000
WORKDIR /tmp
ENTRYPOINT ["/opt/reader-venv/bin/python3", "-I", "/opt/reader/worker.py"]
