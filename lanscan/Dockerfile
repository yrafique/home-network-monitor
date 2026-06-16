FROM python:3.13-alpine

# nmap for scanning, libpcap for raw packet capture
RUN apk add --no-cache nmap nmap-scripts libpcap libpcap-dev tcpdump && \
    ln -sf "$(find /usr/lib -name 'libpcap.so.*' | head -1)" /usr/lib/libpcap.so 2>/dev/null || true

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lanscan.py .

EXPOSE 8001

CMD ["python", "-u", "lanscan.py"]
