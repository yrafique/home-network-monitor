"""Best-effort WhatsApp notifier via CallMeBot or Twilio, controlled by WHATSAPP_PROVIDER."""
import base64
import os
import urllib.parse
import urllib.request

PROVIDER = os.environ.get("WHATSAPP_PROVIDER", "callmebot").lower()
ALERTS_ENABLED = os.environ.get("ALERTS_ENABLED", "false").lower() in ("1", "true", "yes", "on")
TIMEOUT = float(os.environ.get("ALERT_HTTP_TIMEOUT_SECONDS", "10"))

# CallMeBot
CALLMEBOT_PHONE = os.environ.get("CALLMEBOT_PHONE", "")   # intl format, no '+', e.g. 9715XXXXXXXX
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")

# Twilio
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # e.g. whatsapp:+14155238886
TWILIO_TO = os.environ.get("TWILIO_WHATSAPP_TO", "")      # e.g. whatsapp:+9715XXXXXXXX


def _send_callmebot(text: str) -> str:
    if not (CALLMEBOT_PHONE and CALLMEBOT_APIKEY):
        raise RuntimeError("CALLMEBOT_PHONE / CALLMEBOT_APIKEY not set")
    params = urllib.parse.urlencode(
        {"phone": CALLMEBOT_PHONE, "text": text, "apikey": CALLMEBOT_APIKEY}
    )
    url = "https://api.callmebot.com/whatsapp.php?" + params
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return resp.read().decode(errors="replace")[:200]


def _send_twilio(text: str) -> str:
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and TWILIO_TO):
        raise RuntimeError("TWILIO_* credentials not fully set")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    data = urllib.parse.urlencode(
        {"From": TWILIO_FROM, "To": TWILIO_TO, "Body": text}
    ).encode()
    req = urllib.request.Request(url, data=data)
    auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode(errors="replace")[:200]


def send_whatsapp(text: str, force: bool = False) -> bool:
    """Best-effort send. Returns True on success. No-op unless enabled or force."""
    if not (ALERTS_ENABLED or force):
        return False
    try:
        resp = _send_twilio(text) if PROVIDER == "twilio" else _send_callmebot(text)
        print(f"[notify] sent via {PROVIDER}: {text!r} -> {resp!r}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001 - never let a failed alert crash the loop
        print(f"[notify] send failed ({PROVIDER}): {exc}", flush=True)
        return False


if __name__ == "__main__":
    # Manual test: `python notifier.py "hello"` (forces a send regardless of flag)
    import sys

    msg = sys.argv[1] if len(sys.argv) > 1 else "Internet monitor: test alert ✅"
    ok = send_whatsapp(msg, force=True)
    print("OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)
