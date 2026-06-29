import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime

from curl_cffi import requests as cffi_requests
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(DATA_DIR, "data.json")

EVENTIM_API = "https://public-api.eventim.com/websearch/search/api/exploration/v2/productGroups"


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"events": [], "whatsapp": {"phone": "", "apikey": ""}}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def extract_product_id(url):
    """Extract product ID from eventim URL like /event/blink-182-waldbuehne-berlin-21739388/"""
    m = re.search(r"-(\d{6,})/?$", url.rstrip("/"))
    return m.group(1) if m else None


def check_event(url):
    """Check ticket availability via eventim public API.
    Returns (available: bool|None, detail: str).

    Uses two API calls: one with in_stock=true to see what's actually buyable,
    one without to find the event at all. The API's 'status' field is unreliable —
    'Available' just means the event exists, not that tickets are in stock.
    """
    product_id = extract_product_id(url)
    if not product_id:
        return None, "Kann Produkt-ID nicht aus URL extrahieren"

    path = url.rstrip("/").split("/")[-1]
    search_slug = re.sub(r"-\d{6,}$", "", path).replace("-", " ")
    base_params = {"search_term": search_slug, "retail_partner": "EVE", "language": "de"}

    try:
        # Check what's actually in stock
        r = cffi_requests.get(EVENTIM_API, impersonate="chrome", timeout=20,
                              params={**base_params, "in_stock": "true"})
        r.raise_for_status()
        in_stock_data = r.json()
    except Exception as e:
        return None, f"API error: {e}"

    # Find our product in the in_stock results
    for pg in in_stock_data.get("productGroups", []):
        for p in pg.get("products", []):
            if str(p.get("productId")) == product_id:
                tags = p.get("tags", [])
                if "FANSALE" in tags and "TICKETDIRECT" not in tags:
                    return False, "Nur Fansale (Wiederverkauf)"
                return True, "Tickets verfügbar"

    # Not in stock — check if event exists at all
    try:
        r2 = cffi_requests.get(EVENTIM_API, impersonate="chrome", timeout=20, params=base_params)
        r2.raise_for_status()
        all_data = r2.json()
    except Exception as e:
        return None, f"API error: {e}"

    for pg in all_data.get("productGroups", []):
        for p in pg.get("products", []):
            if str(p.get("productId")) == product_id:
                tags = p.get("tags", [])
                if "FANSALE" in tags:
                    return False, "Nur Fansale (Wiederverkauf)"
                return False, "Nicht verfügbar"

    return None, "Event nicht in API gefunden"


def send_whatsapp(phone, apikey, message):
    if not phone or not apikey:
        return False
    encoded = urllib.parse.quote(message)
    url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={encoded}&apikey={apikey}"
    try:
        r = cffi_requests.get(url, impersonate="chrome", timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def run_checks(data):
    """Check all events, send notifications for newly available ones."""
    for event in data["events"]:
        available, detail = check_event(event["url"])
        event["last_check"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        if available is True:
            event["last_status"] = "available"
        elif available is False:
            event["last_status"] = "unavailable"
        else:
            event["last_status"] = "unknown"
        event["last_detail"] = detail

        if available and not event.get("notified"):
            msg = f"Tickets verfuegbar!\n{event.get('name', '')}\n{event['url']}"
            if send_whatsapp(data["whatsapp"]["phone"], data["whatsapp"]["apikey"], msg):
                event["notified"] = True
                event["notified_at"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    save_data(data)


def send_daily_summary(data):
    """Send a daily status summary for all monitored events."""
    if not data["events"]:
        return
    lines = ["Ticket-Check Tagesbericht:"]
    for event in data["events"]:
        status_map = {"available": "VERFUEGBAR", "unavailable": "Nicht verfuegbar",
                      "unknown": "Unklar", "pending": "Ausstehend"}
        status = status_map.get(event.get("last_status", ""), event.get("last_status", ""))
        detail = event.get("last_detail", "")
        lines.append(f"\n{event.get('name', '?')}: {status}")
        if detail:
            lines.append(f"  ({detail})")
    msg = "\n".join(lines)
    send_whatsapp(data["whatsapp"]["phone"], data["whatsapp"]["apikey"], msg)


DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "8"))


def checker_loop():
    """Background: check every 10 minutes, daily summary at configured hour."""
    last_summary_date = None
    while True:
        time.sleep(600)
        try:
            data = load_data()
            run_checks(data)

            today = datetime.now().date()
            now_hour = datetime.now().hour
            if now_hour >= DAILY_SUMMARY_HOUR and last_summary_date != today:
                data = load_data()  # reload after run_checks saved
                send_daily_summary(data)
                last_summary_date = today
        except Exception as e:
            print(f"Checker error: {e}")


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html", data=load_data())


@app.route("/add", methods=["POST"])
def add_event():
    url = request.form.get("url", "").strip()
    name = request.form.get("name", "").strip()
    if url:
        data = load_data()
        data["events"].append({
            "url": url,
            "name": name or url.split("/")[-1].replace("-", " ").title(),
            "added": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "last_check": None, "last_status": "pending", "last_detail": "",
            "notified": False,
        })
        save_data(data)
    return redirect(url_for("index"))


@app.route("/delete/<int:idx>")
def delete_event(idx):
    data = load_data()
    if 0 <= idx < len(data["events"]):
        data["events"].pop(idx)
        save_data(data)
    return redirect(url_for("index"))


@app.route("/reset/<int:idx>")
def reset_notify(idx):
    data = load_data()
    if 0 <= idx < len(data["events"]):
        data["events"][idx]["notified"] = False
        save_data(data)
    return redirect(url_for("index"))


@app.route("/settings", methods=["POST"])
def settings():
    data = load_data()
    data["whatsapp"]["phone"] = request.form.get("phone", "").strip()
    data["whatsapp"]["apikey"] = request.form.get("apikey", "").strip()
    save_data(data)
    return redirect(url_for("index"))


@app.route("/check-now")
def check_now():
    run_checks(load_data())
    return redirect(url_for("index"))


if __name__ == "__main__":
    # ponytail: single background thread, use APScheduler if this grows
    threading.Thread(target=checker_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5555)
