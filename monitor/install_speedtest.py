"""Downloads and installs the Ookla Speedtest CLI binary for the current architecture."""
import os
import platform
import tarfile
import urllib.request

VERSION = os.environ.get("OOKLA_VERSION", "1.2.0")
ARCH = {
    "aarch64": "aarch64", "arm64": "aarch64",
    "x86_64": "x86_64", "amd64": "x86_64",
    "armv7l": "armhf", "armv6l": "armhf",
}.get(platform.machine(), "x86_64")

url = f"https://install.speedtest.net/app/cli/ookla-speedtest-{VERSION}-linux-{ARCH}.tgz"
print(f"[install] fetching {url}")
urllib.request.urlretrieve(url, "/tmp/st.tgz")
with tarfile.open("/tmp/st.tgz") as tar:
    tar.extract(tar.getmember("speedtest"), "/usr/local/bin", filter="data")
os.chmod("/usr/local/bin/speedtest", 0o755)
os.remove("/tmp/st.tgz")
print(f"[install] speedtest CLI installed for {ARCH}")
