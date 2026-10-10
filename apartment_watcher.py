#!/usr/bin/env python3
"""
Heilbronn student apartment watcher.

Checks these pages and alerts you when an apartment becomes available:
  - GEWO Studenten (APP one / APP two)  https://studenten.hn/freie-appartements
  - Böhringer Studentenapartments       https://www.boehringer.net/studentenapartments/
  - Campus Living Heilbronn             https://www.campus-living-heilbronn.de/mieten
  - Rosenberg Quartier                  https://rosenberg-quartier.de/apartments-wohnungen/student-apartments
  - W27 Apartments HN                   https://www.apartments-hn.de/apartment-mieten

Setup:   pip install requests beautifulsoup4
Run:     python apartment_watcher.py            # check every 10 min, forever
         python apartment_watcher.py --once     # single check (for cron / Task Scheduler)
         python apartment_watcher.py --interval 300

Optional push notifications (env vars):
  NTFY_TOPIC=my-secret-topic        -> install the ntfy app and subscribe to that topic
  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
You are only alerted about *new* listings (state is kept in apartment_state.json).
"""
import argparse, json, os, random, re, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).with_name("apartment_state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124 Safari/537.36",
           "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}


class StructureChanged(Exception):
    """Raised when a page no longer looks like we expect (selectors broke)."""


def get_soup(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- site checks
# Each returns a list of strings, one per available unit (empty list = nothing free).

def check_gewo():
    url = "https://studenten.hn/freie-appartements"
    soup = get_soup(url)
    found, sections = [], 0
    for h2 in soup.find_all("h2"):
        title = clean(h2.get_text())
        if not title.startswith("Freie Appartements"):
            continue
        sections += 1
        # collect everything until the next h2
        chunk = []
        for el in h2.find_all_next():
            if el.name == "h2":
                break
            if el.name in ("tr", "p", "li") and not el.find(["tr", "p", "li"]):
                chunk.append(clean(el.get_text(" ")))
        chunk = [c for c in chunk if c]
        if any("keine Angebote" in c for c in chunk):
            continue
        rows = [c for c in chunk if c] or ["(listing present – check page)"]
        found += [f"{title}: {r}" for r in rows]
    if sections == 0:
        raise StructureChanged("no 'Freie Appartements' headings found")
    return found


def check_boehringer():
    url = "https://www.boehringer.net/studentenapartments/"
    soup = get_soup(url)
    box = soup.select_one(".modul-free_rooms")
    if box is None:
        raise StructureChanged("'.modul-free_rooms' section missing")
    text = clean(box.get_text(" "))
    if "keine freien Zimmer" in text:
        return []
    items = [clean(x.get_text(" ")) for x in box.select("tr, li, .free_room, article")]
    items = [i for i in items if i] or [text[:300]]
    return items


def check_campus_living():
    url = "https://www.campus-living-heilbronn.de/mieten"
    soup = get_soup(url)
    units = soup.select("a.apartment")
    if not units:
        raise StructureChanged("no floor-plan apartment buttons found")
    free = {}
    for a in units:
        classes = set(a.get("class", []))
        if classes & {"occupied", "reserved"}:
            continue
        status = "free"
        m = re.search(r"Status:\s*<span[^>]*>([^<]+)", a.get("data-text", ""))
        if m:
            status = m.group(1).strip()
        if status.lower() in ("vermietet", "reserviert"):
            continue
        name = a.get("title") or clean(a.get_text())
        free[name] = f"{name} – {status}"   # dict dedupes desktop+mobile duplicates
    return sorted(free.values())


def check_rosenberg():
    url = "https://rosenberg-quartier.de/apartments-wohnungen/student-apartments"
    soup = get_soup(url)
    tables = soup.select("table[id^=reos-]")
    if not tables:
        raise StructureChanged("no unit tables (table[id^=reos-]) found")
    found = []
    for t in tables:
        h = t.find_previous("h2")
        building = clean(h.get_text()) if h else "?"
        body = t.find("tbody") or t
        for tr in body.find_all("tr"):
            cells = [clean(td.get_text(" ")) for td in tr.find_all("td")]
            if cells:
                found.append(f"{building}: " + " | ".join(cells))
    return found



def parse_w27(payload, today=None):
    """Match W27's Available / Soon available categories (under 6 calendar months)."""
    today = today or datetime.now()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise StructureChanged("W27 API returned no apartment rows; check manually")
    found = []
    for row in rows:
        a = row.get("attributes", {})
        required = ("title", "next_rental_begin", "object_type",
                    "area_living_space", "price_rental_price_flatrate")
        if not all(k in a for k in required):
            raise StructureChanged("W27 apartment fields changed")
        begin = a["next_rental_begin"]
        if begin == "":
            status = "Available now"
        elif isinstance(begin, str):
            try:
                rental_date = datetime.strptime(begin, "%Y-%m-%d")
            except ValueError:
                raise StructureChanged("W27 rental date format changed")
            months = (today.year - rental_date.year) * 12 + today.month - rental_date.month
            # This is the rule used by the site's apartment table, not its banner.
            if abs(months) >= 6:
                continue
            status = f"Soon available from {begin}"
        else:
            raise StructureChanged("W27 rental date is missing or invalid")
        found.append(f"W27 apartment {a['title']} | {a['object_type']} | "
                     f"{a['area_living_space']} sqm | "
                     f"EUR {a['price_rental_price_flatrate']}/month | {status}")
    return sorted(set(found))


def check_w27():
    """Find the site's public API integration so rotated asset names still work."""
    url = "https://www.apartments-hn.de/apartment-mieten"
    soup = get_soup(url)
    def fetch_text(asset_url):
        response = requests.get(asset_url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        return response.text
    main_tag = soup.select_one('script[type="module"][src]')
    if main_tag is None:
        raise StructureChanged("W27 application script missing")
    main_url = urljoin(url, main_tag["src"])
    main_js = fetch_text(main_url)
    route = re.search(r'path:"/apartment-mieten",component:.*?import\("([^"]+)"\)', main_js)
    if not route:
        raise StructureChanged("W27 apartment page module missing")
    page_url = urljoin(main_url, route.group(1))
    page_js = fetch_text(page_url)
    # These modules are public code used by the visitor's browser. Never use a
    # tenant login or user credentials; read only the public apartment-list API.
    modules = list(dict.fromkeys(re.findall(r'(?:from|import)\s*"(\./[^"\s]+\.js)"', page_js)))
    for module in reversed(modules):
        module_url = urljoin(page_url, module)
        if module_url == main_url:
            continue
        code = fetch_text(module_url)
        endpoint = re.search(r'https://live\.api\.schwarz/[^"\s]+/api/', code)
        auth = re.search(r'Authorization:"(Basic [^"]+)"', code)
        if endpoint and auth:
            headers = {**HEADERS, "Authorization": auth.group(1), "Accept": "application/json"}
            response = requests.get(endpoint.group(0) + "apartments/27", headers=headers, timeout=30)
            response.raise_for_status()
            return parse_w27(response.json())
    raise StructureChanged("W27 public listing API integration changed")

SITES = {
    "W27 Apartments HN": ("https://www.apartments-hn.de/apartment-mieten", check_w27),
    "GEWO Studenten": ("https://studenten.hn/freie-appartements", check_gewo),
    "Böhringer": ("https://www.boehringer.net/studentenapartments/", check_boehringer),
    "Campus Living": ("https://www.campus-living-heilbronn.de/mieten", check_campus_living),
    "Rosenberg Quartier": ("https://rosenberg-quartier.de/apartments-wohnungen/student-apartments", check_rosenberg),
}


# -------------------------------------------------------------- notifications

def notify(title, message, url=None):
    print(f"\n🔔 {title}\n{message}\n", flush=True)
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        try:
            headers = {"Title": title.encode("utf-8"), "Priority": "high", "Tags": "house"}
            if url:
                headers["Click"] = url
            requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                          headers=headers, timeout=15)
        except Exception as e:
            print(f"  ntfy failed: {e}")
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": f"{title}\n{message}\n{url or ''}"},
                          timeout=15)
        except Exception as e:
            print(f"  Telegram failed: {e}")
    if sys.platform == "darwin":
        os.system(f"""osascript -e 'display notification "{message[:200]}" with title "{title}"' >/dev/null 2>&1""")
    elif sys.platform.startswith("linux"):
        os.system(f'notify-send "{title}" "{message[:200]}" >/dev/null 2>&1')


# ----------------------------------------------------------------------- main

def load_state():
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def run_once(state):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for name, (url, fn) in SITES.items():
        try:
            listings = fn()
        except StructureChanged as e:
            print(f"[{stamp}] {name}: ⚠️ page layout changed ({e}) – check manually: {url}")
            if state.get(f"{name}__warned") != str(e):
                notify(f"⚠️ {name}: watcher needs update", f"{e}", url)
                state[f"{name}__warned"] = str(e)
            continue
        except Exception as e:
            print(f"[{stamp}] {name}: error ({e.__class__.__name__}: {e})")
            continue
        state.pop(f"{name}__warned", None)
        seen = set(state.get(name, []))
        new = [l for l in listings if l not in seen]
        print(f"[{stamp}] {name}: {len(listings)} available" + (f", {len(new)} NEW" if new else ""))
        if new:
            notify(f"🏠 {name}: {len(new)} apartment(s) available!",
                   "\n".join(new[:15]) + (f"\n…and {len(new) - 15} more" if len(new) > 15 else ""),
                   url)
        state[name] = listings
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--interval", type=int, default=600, help="seconds between checks (default 600)")
    p.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = p.parse_args()
    if args.test_notify:
        notify("Apartment watcher test", "Notifications work 🎉")
        return
    state = load_state()
    while True:
        run_once(state)
        if args.once:
            break
        time.sleep(args.interval + random.randint(0, 60))   # small jitter, be polite


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
