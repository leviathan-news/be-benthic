FROM python:3.12-slim

# Install analysis and onchain libraries — no shell tools (curl, wget, git)
RUN pip install --no-cache-dir \
    web3 requests pandas matplotlib eth-abi

# Pre-built helpers and chain config — importable as `from helpers import ...`
COPY helpers.py chains.json /usr/local/lib/python3.12/site-packages/

# Non-root user for container execution
RUN useradd -m sandbox
USER sandbox
WORKDIR /tmp
