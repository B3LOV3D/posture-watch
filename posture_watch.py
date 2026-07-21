"""
Paris Bike Co - Surveillance de creneaux (etude posturale velo de route)

Surveille la page de reservation Acuity Scheduling et notifie (via ntfy.sh)
des qu'un NOUVEAU jour devient disponible.

Detection: les jours cliquables du calendrier sont des <button> HTML non
desactives dont le texte est un simple nombre (1-31). On compare, pour
chaque mois consulte, la liste des jours disponibles avec celle de la
verification precedente.

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
    "Tout accepter", "Accepter", "J'accepte",
    "Accept all", "Accept", "OK",
]

NEXT_MONTH_HINTS = ["next", "suivant", "prochain"]

# Message specifique affiche par Acuity pour UN MOIS DONNE quand il est
# complet (ne pas confondre avec le texte d'avertissement general en haut
# de page, qui contient aussi le mot "complet" mais s'affiche tout le
# temps, disponibilite ou non - piege qu'on evite en ne s'en servant plus
# comme critere de decision, juste comme info de log).
NO_AVAILABILITY_HINT = re.compile(r"no appointments? (are|is) available", re.IGNORECASE)

MONTH_NAME_PATTERN = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December"
    r"|janvier|f[eé]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[eé]cembre)"
    r"\s+\d{4}",
    re.IGNORECASE,
)


def dismiss_cookie_banner(page) -> None:
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


def collect_available_days(page) -> set:
    """Jours (nombres 1-31) dont le bouton calendrier est cliquable
    (non desactive) - donc disponibles a la reservation."""
    days = set()
    try:
        for b in page.query_selector_all("button:not([disabled])"):
            label = (b.get_attribute("aria-label") or b.inner_text() or "").strip()
            if label and len(label) < 20 and re.fullmatch(r"([1-9]|[12]\d|3[01])", label):
                days.add(label)
    except Exception:
        pass
    return days


def find_next_month_button(page):
    for b in page.query_selector_all("button"):
        label = (b.get_attribute("aria-label") or b.inner_text() or "").lower()
        if any(hint in label for hint in NEXT_MONTH_HINTS):
            return b
    return None


def fetch_month_states() -> dict:
    """Retourne {mois_label: [jours disponibles (str), tries]}"""
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
            days = collect_available_days(page)
            no_avail_message_present = bool(NO_AVAILABILITY_HINT.search(text))

            states[label] = sorted(days, key=int)
            debug_lines.append(
                f"Mois #{month_index} ({label}): jours disponibles = {sorted(days, key=int)} "
                f"(message 'complet' Acuity present: {no_avail_message_present})"
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


def notify(newly_open: dict) -> None:
    lines = [f"{month}: jour(s) {', '.join(days)}" for month, days in newly_open.items()]
    message = "Nouveau(x) creneau(x) disponible(s) !\n" + "\n".join(lines)
    message += "\n\nClique pour reserver avant que ca parte."

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

    newly_open = {}
    for month, days in current_states.items():
        previous_days = set(previous_states.get(month, []))
        new_days = sorted(set(days) - previous_days, key=int)
        if new_days:
            newly_open[month] = new_days

    print(f"Etat actuel: {current_states}")

    if newly_open:
        print(f"Nouveau(x): {newly_open}")
        notify(newly_open)
    else:
        print("Rien de nouveau depuis la derniere verification.")

    save_state(current_states)


if __name__ == "__main__":
    sys.exit(main() or 0)
