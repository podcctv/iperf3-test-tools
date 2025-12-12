FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y iperf3 curl jq bc netcat-openbsd dnsutils iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Allow pip index to be configured at build time
# Default to PyPI; Chinese servers should pass --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple/
ARG PIP_TRUSTED_HOST=""

COPY requirements.txt /app/
RUN if [ -n "$PIP_TRUSTED_HOST" ]; then \
      pip install --no-cache-dir -r requirements.txt \
        -i "$PIP_INDEX_URL" \
        --trusted-host "$PIP_TRUSTED_HOST" \
        --timeout 120; \
    else \
      pip install --no-cache-dir -r requirements.txt \
        -i "$PIP_INDEX_URL" \
        --timeout 120; \
    fi

COPY app.py /app/
COPY ip_whitelist.py /app/

# Create data directory for whitelist persistence
RUN mkdir -p /app/data

EXPOSE 8000 62001/tcp 62001/udp

CMD ["python", "app.py"]
