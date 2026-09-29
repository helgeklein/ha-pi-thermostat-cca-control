FROM mcr.microsoft.com/devcontainers/python:3.14

USER root

# Ruby supports the Jekyll documentation site.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ruby-full bundler build-essential zlib1g-dev ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-ha.txt requirements-ruff.txt requirements-test.txt /tmp/pi-thermostat-requirements/
RUN python3 -m pip install --no-user \
    --constraint /tmp/pi-thermostat-requirements/requirements-test.txt \
    --requirement /tmp/pi-thermostat-requirements/requirements.txt \
    && python3 -m pip install --no-user \
    --requirement /tmp/pi-thermostat-requirements/requirements-test.txt

USER vscode