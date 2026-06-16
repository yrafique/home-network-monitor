FROM python:3.12-slim

# No OS packages needed: ICMP is done in Python via icmplib (raw sockets +
# the container's NET_RAW capability), so there is no apt step at all.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ookla Speedtest CLI for throughput + bufferbloat (downloaded over HTTPS).
COPY install_speedtest.py .
RUN python install_speedtest.py && rm install_speedtest.py

COPY monitor.py analyzer.py outage_logger.py notifier.py path.py loadtest.py enrich.py ./

EXPOSE 8000

CMD ["python", "-u", "monitor.py"]
