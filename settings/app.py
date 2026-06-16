import os, re, json
from flask import Flask, render_template, request, jsonify
import docker

app = Flask(__name__)

ENV_PATH = os.environ.get("ENV_PATH", "/config/.env")
STACK_NAME = os.environ.get("COMPOSE_PROJECT", "internet")

SCHEMA = [
    {
        "id": "targets",
        "title": "Probe Targets",
        "icon": "🎯",
        "settings": [
            {"key": "PING_TARGETS",  "label": "Ping Targets",  "type": "tags",
             "hint": "IPs or hostnames to ICMP-ping. Add/remove to change what's monitored.",
             "restart": ["monitor"]},
            {"key": "DNS_TARGETS",   "label": "DNS Targets",   "type": "tags",
             "hint": "Domains tested for DNS resolution time and success.",
             "restart": ["monitor"]},
            {"key": "HTTP_TARGETS",  "label": "HTTP Targets",  "type": "tags",
             "hint": "Full URLs checked for HTTP reachability and response time.",
             "restart": ["monitor"]},
            {"key": "PATH_TARGETS",  "label": "Path Targets",  "type": "tags",
             "hint": "Targets for per-hop traceroute (MTR-style). Usually one WAN IP.",
             "restart": ["monitor"]},
        ],
    },
    {
        "id": "timing",
        "title": "Probe Timing",
        "icon": "⏱",
        "settings": [
            {"key": "PROBE_INTERVAL_SECONDS",       "label": "Probe interval",       "type": "number", "unit": "s",  "min": 5,   "max": 300, "restart": ["monitor"]},
            {"key": "PING_COUNT",                   "label": "Pings per probe",       "type": "number", "unit": "",   "min": 1,   "max": 50,  "restart": ["monitor"]},
            {"key": "PING_PACKET_INTERVAL_SECONDS", "label": "Ping packet interval",  "type": "number", "unit": "s",  "min": 0.1, "max": 2,   "step": "0.1", "restart": ["monitor"]},
            {"key": "PING_PACKET_TIMEOUT_SECONDS",  "label": "Ping packet timeout",   "type": "number", "unit": "s",  "min": 0.5, "max": 10,  "step": "0.5", "restart": ["monitor"]},
            {"key": "HTTP_TIMEOUT_SECONDS",         "label": "HTTP timeout",          "type": "number", "unit": "s",  "min": 1,   "max": 30,  "restart": ["monitor"]},
            {"key": "PATH_INTERVAL_SECONDS",        "label": "Path probe interval",   "type": "number", "unit": "s",  "min": 10,  "max": 600, "restart": ["monitor"]},
            {"key": "PATH_COUNT",                   "label": "Path packets per hop",  "type": "number", "unit": "",   "min": 1,   "max": 20,  "restart": ["monitor"]},
        ],
    },
    {
        "id": "speedtest",
        "title": "Speedtest",
        "icon": "⚡",
        "settings": [
            {"key": "LOADTEST_ENABLED",          "label": "Speedtest enabled",    "type": "toggle",
             "hint": "Disable on metered links. Each run uses ~100–500 MB of bandwidth.",
             "restart": ["monitor"]},
            {"key": "LOADTEST_INTERVAL_SECONDS", "label": "Speedtest interval",   "type": "number", "unit": "s", "min": 300, "max": 86400, "restart": ["monitor"]},
            {"key": "LOADTEST_START_DELAY_SECONDS", "label": "Start delay",       "type": "number", "unit": "s", "min": 0,   "max": 300,   "restart": ["monitor"]},
            {"key": "SPEEDTEST_SERVER_ID",       "label": "Pin Ookla server ID",  "type": "text",
             "hint": "Leave blank to let Ookla pick the nearest server automatically.",
             "restart": ["monitor"]},
        ],
    },
    {
        "id": "seasonal",
        "title": "Seasonal Analysis",
        "icon": "📈",
        "settings": [
            {"key": "SEASONAL_WINDOW_DAYS",    "label": "Baseline window",   "type": "number", "unit": "days", "min": 3,  "max": 90,   "restart": ["monitor"]},
            {"key": "SEASONAL_MIN_SAMPLES",    "label": "Min samples",       "type": "number", "unit": "",     "min": 3,  "max": 50,   "restart": ["monitor"],
             "hint": "Minimum historical samples before publishing a seasonal expectation."},
            {"key": "SEASONAL_REFRESH_SECONDS","label": "Refresh interval",  "type": "number", "unit": "s",    "min": 60, "max": 3600, "restart": ["monitor"]},
            {"key": "SEASONAL_STEP_SECONDS",   "label": "History step",      "type": "number", "unit": "s",    "min": 60, "max": 600,  "restart": ["monitor"]},
        ],
    },
    {
        "id": "alerts",
        "title": "Alerts",
        "icon": "🔔",
        "settings": [
            {"key": "ALERTS_ENABLED",    "label": "WhatsApp alerts",  "type": "toggle",
             "hint": "Sends a WhatsApp message on outage start and recovery.",
             "restart": ["monitor"]},
            {"key": "ALERT_TARGETS",     "label": "Alert on targets", "type": "tags",
             "hint": "Which targets trigger an alert. 'internet' = all-providers-down.",
             "restart": ["monitor"]},
            {"key": "ALERT_TZ",          "label": "Alert timezone",   "type": "text",
             "hint": "IANA timezone for alert timestamps, e.g. America/Toronto",
             "restart": ["monitor"]},
            {"key": "WHATSAPP_PROVIDER", "label": "WhatsApp provider","type": "select",
             "options": ["callmebot", "twilio"], "restart": ["monitor"]},
            {"key": "CALLMEBOT_PHONE",   "label": "CallMeBot phone",  "type": "text",
             "hint": "International format without +, e.g. 14155551234"},
            {"key": "CALLMEBOT_APIKEY",  "label": "CallMeBot API key","type": "text"},
        ],
    },
    {
        "id": "grafana",
        "title": "Grafana",
        "icon": "📊",
        "settings": [
            {"key": "GRAFANA_USER",     "label": "Admin username", "type": "text",    "restart": ["grafana"]},
            {"key": "GRAFANA_PASSWORD", "label": "Admin password", "type": "password","restart": ["grafana"]},
        ],
    },
    {
        "id": "misc",
        "title": "Advanced",
        "icon": "⚙️",
        "settings": [
            {"key": "ENRICH", "label": "Hostname enrichment", "type": "toggle",
             "hint": "Resolve IPs to owner labels (Google, Cloudflare, etc.) via rDNS and ipinfo.io.",
             "restart": ["monitor"]},
        ],
    },
]


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in open(ENV_PATH):
            line = line.split("#")[0].strip()
            if "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def write_env(updates: dict[str, str]) -> None:
    try:
        lines = open(ENV_PATH).readlines()
    except FileNotFoundError:
        lines = []

    written: set[str] = set()
    out = []
    for line in lines:
        stripped = line.split("#")[0].strip()
        if "=" in stripped:
            k = stripped.split("=")[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}\n")
                written.add(k)
                continue
        out.append(line)

    for k, v in updates.items():
        if k not in written:
            out.append(f"{k}={v}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(out)



def get_container_status() -> dict[str, str]:
    status: dict[str, str] = {}
    try:
        client = docker.from_env()
        for c in client.containers.list(all=True):
            name = c.name.replace(f"{STACK_NAME}-", "")
            status[name] = c.status
    except Exception:
        pass
    return status


def restart_containers(names: list[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    try:
        client = docker.from_env()
        for name in names:
            full = f"{STACK_NAME}-{name}"
            try:
                c = client.containers.get(full)
                c.restart(timeout=10)
                results[name] = "restarted"
            except docker.errors.NotFound:
                results[name] = "not found"
            except Exception as e:
                results[name] = f"error: {e}"
    except Exception as e:
        return {"error": str(e)}
    return results



@app.route("/")
def index():
    env = read_env()
    status = get_container_status()
    return render_template("index.html", schema=SCHEMA, env=env, status=status)


@app.route("/api/save", methods=["POST"])
def save():
    data = request.get_json(force=True)
    updates = data.get("settings", {})
    restart = data.get("restart", [])

    write_env(updates)

    restart_results = {}
    if restart:
        restart_results = restart_containers(restart)

    return jsonify({"ok": True, "restarted": restart_results})


@app.route("/api/status")
def status():
    return jsonify(get_container_status())


@app.route("/api/restart", methods=["POST"])
def restart():
    names = request.get_json(force=True).get("containers", [])
    return jsonify(restart_containers(names))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
