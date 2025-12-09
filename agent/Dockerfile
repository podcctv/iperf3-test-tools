FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y iperf3 curl jq bc netcat-openbsd dnsutils iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/
COPY ip_whitelist.py /app/

# Create data directory for whitelist persistence
RUN mkdir -p /app/data

EXPOSE 8000 62001/tcp 62001/udp

CMD ["python", "app.py"]
