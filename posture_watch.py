"""
Paris Bike Co - Surveillance de creneaux (etude posturale velo de route)

Surveille la page de reservation Acuity Scheduling et notifie (via ntfy.sh)
des qu'un mois qui etait complet ("No appointments are available this
month.") a nouveau des places.

On detecte la disponibilite au niveau du MOIS (signal fiable: Acuity
affiche explicitement "No appointments are available this month." quand
un mois est complet), plutot qu'au niveau du jour individuel (les jours
ne sont pas de simples boutons HTML standards, donc peu fiables a
detecter directement). Une capture d'ecran de chaque mois consulte est
sauvegardee, pour pouvoir reperer les dates exactes a l'oeil.

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
SCREENSHOT_PREFIX = "posture_month"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC_POSTURE")

# Nombre de fois qu'on clique sur "mois suivant" (0 = uniquement le mois
# affiche au chargement de la page).
LOOKAHEAD_CLICKS = 2

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

COOKIE_BUTTON_TEXTS = [
    "Tout accepter", "Accepter", "J'accepte", "J'accepte",
    "Accept all", "Accept", "OK",
]

NEXT_MONTH_HINTS = ["next", "suivant", "prochain"]

NO_AVAILABILITY_PATTERNS = [
    re.compile(r"no appointments? (are|is) available", re.IGNORECASE),
    re.compile(r"no availability", re.IGNORECASE),
    re.compile(r"aucun[e]? (rendez-vous|creneau|cr[eé]neau)[^.]{0,40}disponible", re.IGNORECASE),
    re.compile(r"complet", re.IGNORECASE),
]

MONTH_NAME_PATTERN = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December"
    r"|janvier|f[eé]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[eé]cembre)"
    r"\s+\d{4}",
    re.IGNORECASE,
)


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


def get_page_text(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def month_label(page_text: str, fallback_index: int) -> str:
    m = MONTH_NAME_PATTERN.search(page_text)
    if m:
        return m.group(0)
    return f"mois_inconnu_{fallback_index}"


def month_has_availability(page_text: str) -> bool:
    for pat in NO_AVAILABILITY_PATTERNS:
        if pat.search(page_text):
            return False
    return True


def count_clickable_day_buttons(page) -> int:
    """Info de debug seulement (pas utilise pour la decision de notif):
    compte les <button> non desactives dont le texte ressemble a un
    numero de jour. Utile pour calibrer si Acuity change de structure."""
    count = 0
    try:
        for b in page.query_selector_all("button:not([disabled])"):
            label = (b.get_attribute("aria-label") or b.inner_text() or "").strip()
            if label and len(label) < 20 and re.fullmatch(r"([1-9]|[12]\d|3[01])", label):
                count += 1
    except Exception:
        pass
    return count


def find_next_month_button(page):
    for b in page.query_selector_all("button"):
        label = (b.get_attribute("aria-label") or b.inner_text() or "").lower()
        if any(hint in label for hint in NEXT_MONTH_HINTS):
            return b
    return None


def fetch_month_states() -> dict:
    """Retourne un dict {mois_label: True/False (disponibilite)} pour le
    mois affiche au chargement + les LOOKAHEAD_CLICKS mois suivants."""
    states = {}
    debug_lines = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA, viewport={"width": 1300, "height": 1100})
        page.goto(APPOINTMENT_URL, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)

        dismiss_cookie_banner(page)
        page.wait_for_timeout(500)

        for month_index in range(LOOKAHEAD_CLICKS + 1):
            text = get_page_text(page)
            label = month_label(text, month_index)
            has_avail = month_has_availability(text)
            day_buttons = count_clickable_day_buttons(page)

            states[label] = has_avail
            debug_lines.append(
                f"Mois #{month_index} ({label}): disponibilite={has_avail} "
                f"(boutons jour detectes en plus, info: {day_buttons})"
            )

            screenshot_path = f"{SCREENSHOT_PREFIX}_{month_index}.png"
            try:
                page.screenshot(path=screenshot_path, full_page=True)
            except Exception as e:
                debug_lines.append(f"Capture d'ecran impossible pour ce mois: {e}")

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

        browser.close()

    print("\n".join(debug_lines))
    return states


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(states: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(states, f, indent=2, ensure_ascii=False)


def notify(newly_open_months: list) -> None:
    message = "Nouvelle disponibilite detectee pour:\n" + "\n".join(newly_open_months)
    message += "\n\nOuvre le lien pour voir les dates exactes et reserver."

    if not NTFY_TOPIC:
        print(f"[SANS NOTIF - NTFY_TOPIC_POSTURE absent] {message}")
        return

    headers = {
        "Title": "Etude posturale velo route - creneau dispo !",
        "Priority": "urgent",
        "Tags": "bike,rotating_light",
        "Click": APPOINTMENT_URL,
        "Message": message.encode("utf-8"),
    }

    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=20,
        )
        print("Notification envoyee.")
    except Exception as e:
        print(f"Echec de la notification: {e}")


def main() -> None:
    current_states = fetch_month_states()
    previous_states = load_state()

    newly_open = [
        label for label, has_avail in current_states.items()
        if has_avail and not previous_states.get(label, False)
    ]

    print(f"Etat actuel: {current_states}")

    if newly_open:
        print(f"Nouvelle(s) disponibilite(s): {newly_open}")
        notify(newly_open)
    else:
        print("Rien de nouveau depuis la derniere verification.")

    save_state(current_states)


if __name__ == "__main__":
    sys.exit(main() or 0)
