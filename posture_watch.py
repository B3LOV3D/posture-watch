"""
Paris Bike Co - Surveillance de creneaux (etude posturale velo de route)

Surveille la page de reservation Acuity Scheduling et notifie (via ntfy.sh)
des qu'une NOUVELLE date/creneau devient disponible.

Contrairement a FREITAG, ce calendrier est une application JavaScript
dynamique: on utilise donc un vrai navigateur (Playwright) pour le lire,
et on prend une capture d'ecran du calendrier a joindre a la notification
pour verification visuelle rapide.

Variables d'environnement:
    NTFY_TOPIC_POSTURE  -> nom du topic ntfy.sh (obligatoire)
"""

import os
import re
import json
import sys
import requests
from playwright.sync_api import sync_playwright

APPOINTMENT_URL = "https://parisbikeco.as.me/schedule/5d715376/appointment/40208624/calendar/7783875"
STATE_FILE = "posture_last_available.json"
SCREENSHOT_FILE = "posture_calendar.png"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC_POSTURE")

# Nombre de fois qu'on clique sur "mois suivant" pour regarder plus loin
# dans le temps (0 = seulement le mois actuellement affiche).
LOOKAHEAD_CLICKS = 2

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

COOKIE_BUTTON_TEXTS = [
    "Tout accepter", "Accepter", "J'accepte", "J’accepte",
    "Accept all", "Accept", "OK",
]

NEXT_MONTH_HINTS = ["next", "suivant", "prochain"]


def dismiss_cookie_banner(page) -> None:
    """Ferme la banniere de cookies si elle est presente (best-effort)."""
    for text in COOKIE_BUTTON_TEXTS:
        try:
            btn = page.get_by_text(text, exact=False).first
            if btn.is_visible(timeout=800):
                btn.click(timeout=800)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue


def get_calendar_scope(page):
    """Essaie de restreindre la recherche a la zone du calendrier, pour
    eviter de confondre d'autres boutons de la page (menu, cookies, etc.)
    avec des jours disponibles. Si rien de reconnaissable n'est trouve,
    on retombe sur la page entiere."""
    for sel in ["[class*='calendar' i]", "[class*='datepicker' i]", "[role='grid']"]:
        el = page.query_selector(sel)
        if el:
            return el
    return page


def collect_available_labels(page) -> set:
    """Renvoie l'ensemble des libelles (texte ou aria-label) des boutons
    cliquables (= non desactives) dans la zone du calendrier, qui
    ressemblent a un jour du mois (contiennent un nombre 1-31)."""
    scope = get_calendar_scope(page)
    found = set()
    buttons = scope.query_selector_all("button:not([disabled])")
    for b in buttons:
        label = (b.get_attribute("aria-label") or b.inner_text() or "").strip()
        if not label or len(label) > 60:
            continue
        if re.search(r"\b([1-9]|[12]\d|3[01])\b", label):
            found.add(label)
    return found


def find_next_month_button(page):
    for b in page.query_selector_all("button"):
        label = (b.get_attribute("aria-label") or b.inner_text() or "").lower()
        if any(hint in label for hint in NEXT_MONTH_HINTS):
            return b
    return None


def fetch_available_dates():
    all_labels = set()
    debug_lines = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA, viewport={"width": 1300, "height": 1100})
        page.goto(APPOINTMENT_URL, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)

        dismiss_cookie_banner(page)
        page.wait_for_timeout(500)

        for month_index in range(LOOKAHEAD_CLICKS + 1):
            labels = collect_available_labels(page)
            debug_lines.append(
                f"Mois #{month_index}: {len(labels)} bouton(s) 'jour disponible' trouve(s) -> {sorted(labels)}"
            )
            all_labels |= labels

            if month_index < LOOKAHEAD_CLICKS:
                next_btn = find_next_month_button(page)
                if not next_btn:
                    debug_lines.append("Bouton 'mois suivant' introuvable, arret de la navigation dans le temps.")
                    break
                try:
                    next_btn.click(timeout=3000)
                    page.wait_for_timeout(1500)
                except Exception as e:
                    debug_lines.append(f"Impossible de cliquer sur 'mois suivant': {e}")
                    break

        page.screenshot(path=SCREENSHOT_FILE, full_page=True)
        browser.close()

    print("\n".join(debug_lines))
    return all_labels


def load_state() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_state(labels: set) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(labels), f, indent=2, ensure_ascii=False)


def notify(new_labels: set) -> None:
    message = "Nouveau(x) creneau(x) possible(s):\n" + "\n".join(sorted(new_labels))

    if not NTFY_TOPIC:
        print(f"[SANS NOTIF - NTFY_TOPIC_POSTURE absent] {message}")
        return

    headers = {
        "Title": "Etude posturale velo route - creneau dispo",
        "Priority": "high",
        "Tags": "bike,rotating_light",
        "Click": APPOINTMENT_URL,
        "Filename": "calendrier.png",
        "Message": message.encode("utf-8"),
    }

    try:
        if os.path.exists(SCREENSHOT_FILE):
            with open(SCREENSHOT_FILE, "rb") as f:
                requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=f.read(), headers=headers, timeout=20)
        else:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={k: v for k, v in headers.items() if k not in ("Filename",)},
                timeout=20,
            )
        print("Notification envoyee.")
    except Exception as e:
        print(f"Echec de la notification: {e}")


def main() -> None:
    current_labels = fetch_available_dates()
    previous_labels = load_state()

    new_labels = current_labels - previous_labels

    print(f"{len(current_labels)} creneau(x)/jour(s) disponible(s) actuellement.")
    if new_labels:
        print(f"Nouveau(x) depuis la derniere verification: {sorted(new_labels)}")
        notify(new_labels)
    else:
        print("Rien de nouveau depuis la derniere verification.")

    save_state(current_labels)


if __name__ == "__main__":
    sys.exit(main() or 0)
