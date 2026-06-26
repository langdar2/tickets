import json
import os
import threading
import time
import urllib.parse
from datetime import datetime

from curl_cffi import requests as cffi_requests
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(DATA_DIR, "data.json")

UNAVAILABLE = ["ausverkauft", "sold out", "nicht verfügbar", "currently not available",
               "no tickets available", "leider keine tickets"]
AVAILABLE = ["in den warenkorb", "tickets sichern", "karten kaufen", "jetzt buchen",
             "jetzt tickets sichern"]


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"events": [], "whatsapp": {"phone": "", "apikey": ""}}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def check_event(url):
    """Return (available: bool|None, detail: str)."""
    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=20, allow_redirects=False)
    except Exception as e:
        return None, f"error: {e}"

    # Queue/waiting room redirect = event is live/hot, likely selling
    location = resp.headers.get("location", "")
    if resp.status_code in (301, 302) and "queue" in location.lower():
        return None, "Warteschlange aktiv (Event ist live)"

    # Follow redirect manually if it's a normal one
    if resp.status_code in (301, 302) and "queue" not in location.lower():
        try:
            resp = cffi_requests.get(location, impersonate="chrome", timeout=20, allow_redirects=False)
        except Exception as e:
            return None, f"error bei redirect: {e}"

    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"

    html = resp.text.lower()

    for signal in UNAVAILABLE:
        if signal in html:
            return False, signal

    for signal in AVAILABLE:
        if signal in html:
            return True, signal

    return None, "status unklar"


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
        elif "warteschlange" in detail.lower():
            event["last_status"] = "queue"
        else:
            event["last_status"] = "unknown"
        event["last_detail"] = detail

        should_notify = (available and not event.get("notified")) or \
                        (event["last_status"] == "queue" and not event.get("notified"))
        if should_notify:
            if available:
                msg = f"Tickets verfuegbar!\n{event.get('name', '')}\n{event['url']}"
            else:
                msg = f"Warteschlange aktiv! Tickets koennten verfuegbar sein.\n{event.get('name', '')}\n{event['url']}"
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
