FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y iperf3 curl jq bc netcat-openbsd dnsutils iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/

EXPOSE 8000 5201/tcp 5201/udp

CMD ["python", "app.py"]
