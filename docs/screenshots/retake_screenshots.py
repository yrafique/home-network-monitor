#!/usr/bin/env python3
"""Retake key Grafana dashboard screenshots."""

from playwright.sync_api import sync_playwright

BASE_URL = "http://homebridge.local:3000/d/internet-health/internet-health-monitor"
OUT_DIR = "/Users/yousef/Dev/internet/docs/screenshots"

SCREENSHOTS = [
    {
        "filename": "01-dashboard-overview.png",
        "url": f"{BASE_URL}?orgId=1&from=now-24h&to=now&kiosk",
        "viewport": {"width": 1600, "height": 900},
        "extra_wait": 6000,
        "scroll_y": None,
        "scroll_wait": None,
        "clip": None,
    },
    {
        "filename": "02-status-row.png",
        "url": f"{BASE_URL}?orgId=1&from=now-24h&to=now&kiosk",
        "viewport": {"width": 1600, "height": 220},
        "extra_wait": 5000,
        "scroll_y": None,
        "scroll_wait": None,
        "clip": None,
    },
    {
        "filename": "03-latency-timeline.png",
        "url": f"{BASE_URL}?orgId=1&from=now-24h&to=now&kiosk",
        "viewport": {"width": 1600, "height": 660},
        "extra_wait": 5000,
        "scroll_y": 175,
        "scroll_wait": 2000,
        "clip": {"x": 0, "y": 0, "width": 1600, "height": 660},
    },
    {
        "filename": "06-bufferbloat.png",
        "url": f"{BASE_URL}?orgId=1&from=now-24h&to=now&viewPanel=25&kiosk",
        "viewport": {"width": 600, "height": 350},
        "extra_wait": 3000,
        "scroll_y": None,
        "scroll_wait": None,
        "clip": None,
    },
    {
        "filename": "07-anomaly-zscore.png",
        "url": f"{BASE_URL}?orgId=1&from=now-24h&to=now&viewPanel=13&kiosk",
        "viewport": {"width": 1300, "height": 480},
        "extra_wait": 3000,
        "scroll_y": None,
        "scroll_wait": None,
        "clip": None,
    },
    {
        "filename": "08-lan-devices.png",
        "url": f"{BASE_URL}?orgId=1&from=now-24h&to=now&viewPanel=35&kiosk",
        "viewport": {"width": 1400, "height": 540},
        "extra_wait": 4000,
        "scroll_y": None,
        "scroll_wait": None,
        "clip": None,
    },
]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for shot in SCREENSHOTS:
            context = browser.new_context(viewport=shot["viewport"])
            page = context.new_page()

            page.goto(shot["url"])
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(shot["extra_wait"])

            if shot["scroll_y"] is not None:
                page.evaluate(f"window.scrollTo(0, {shot['scroll_y']})")
                page.wait_for_timeout(shot["scroll_wait"])

            out_path = f"{OUT_DIR}/{shot['filename']}"
            page.screenshot(
                path=out_path,
                clip=shot["clip"],
                full_page=False,
            )
            print(f"✓ {shot['filename']}")

            page.close()
            context.close()

        browser.close()


if __name__ == "__main__":
    main()
