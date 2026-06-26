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
    """
    product_id = extract_product_id(url)
    if not product_id:
        return None, "Kann Produkt-ID nicht aus URL extrahieren"

    # Extract search term from URL path for API query
    path = url.rstrip("/").split("/")[-1]  # e.g. blink-182-waldbuehne-berlin-21739388
    # Remove product ID suffix and convert dashes to spaces
    search_slug = re.sub(r"-\d{6,}$", "", path).replace("-", " ")

    try:
        r = cffi_requests.get(EVENTIM_API, impersonate="chrome", timeout=20, params={
            "search_term": search_slug,
            "retail_partner": "EVE",
            "language": "de",
        })
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return None, f"API error: {e}"

    # Find our product in results
    for pg in data.get("productGroups", []):
        for p in pg.get("products", []):
            if str(p.get("productId")) == product_id:
                status = p.get("status", "")
                tags = p.get("tags", [])
                fansale_only = "FANSALE" in tags and status == "Available"

                # ponytail: FANSALE tag = resale only, not regular tickets
                if fansale_only and "TICKETDIRECT" in tags:
                    # Has both regular and fansale - could be either
                    # Check if there are non-FANSALE products in same group
                    other_products = [op for op in pg.get("products", [])
                                      if str(op.get("productId")) != product_id
                                      and "FANSALE" not in op.get("tags", [])]
                    if not other_products:
                        return False, f"Nur Fansale (Wiederverkauf)"

                if status == "Available" and "FANSALE" not in tags:
                    return True, "Tickets verfügbar (regulär)"
                elif status == "Available" and "FANSALE" in tags:
                    return False, "Nur Fansale (Wiederverkauf)"
                elif status == "SoldOut":
                    return False, "Ausverkauft"
                elif status == "Cancelled":
                    return False, "Abgesagt"
                else:
                    return None, f"Status: {status}"

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


def checker_loop():
    """Background: check every 10 minutes."""
    while True:
        time.sleep(600)
        try:
            run_checks(load_data())
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
