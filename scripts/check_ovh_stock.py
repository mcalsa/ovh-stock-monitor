import json
import logging
import os
import sys
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OVH_API_URL = os.environ.get("OVH_API_URL", "https://eu.api.ovh.com/v1/vps/order/rule/datacenter")
OVH_SUBSIDIARY = os.environ.get("OVH_SUBSIDIARY", "WE")

PLAN_CODES = [p.strip() for p in os.environ.get(
    "OVH_PLAN_CODES", "vps-2027-model2,vps-2027-model3,vps-2027-model4"
).split(",") if p.strip()]

TARGET_DATACENTERS = [d.strip() for d in os.environ.get(
    "OVH_TARGET_DATACENTERS", "GRA,SBG,EU-WEST-RBX"
).split(",") if d.strip()]

PLAN_LABELS = {
    "vps-2027-model2": "VPS Model 2",
    "vps-2027-model3": "VPS Model 3",
    "vps-2027-model4": "VPS Model 4",
}

LOCATION_LABELS = {
    "GRA": "Francia - Gravelines",
    "SBG": "Francia - Estrasburgo",
    "EU-WEST-RBX": "Francia - Roubaix",
}

CONFIGURATOR_URL_TEMPLATE = os.environ.get(
    "OVH_CONFIGURATOR_URL_TEMPLATE",
    "https://www.ovhcloud.com/es-es/vps/configurator/?planCode={plan}.LZ&brick=VPS%2BModel%2B2&pricing=upfront12&processor=%20&vcore=4__vCore&storage=75__SSD__NVMe",
)

STATE_FILE = Path(os.environ.get("STATE_FILE", ".state/ovh-stock.json"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
FORCE_STARTUP_MESSAGE = os.environ.get("OVH_FORCE_STARTUP_MESSAGE", "false").strip().lower() == "true"


def telegram_send(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID; no se puede avisar")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Fallo enviando a Telegram: %s", exc)
        return False


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:  # noqa: BLE001
            log.warning("Archivo de estado ilegible, empezando de cero")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_datacenters(plan_code: str) -> list:
    params = {"ovhSubsidiary": OVH_SUBSIDIARY, "planCode": plan_code}
    r = requests.get(OVH_API_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("datacenters", [])


def set_github_output(name: str, value: str) -> None:
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main() -> int:
    state = load_state()
    first_run = not state.get("_initialized", False)
    state_changed = False

    for plan in PLAN_CODES:
        plan_label = PLAN_LABELS.get(plan, plan)
        try:
            datacenters = fetch_datacenters(plan)
        except Exception as exc:  # noqa: BLE001
            log.error("Fallo consultando %s: %s", plan, exc)
            continue

        by_code = {dc["datacenter"]: dc for dc in datacenters}

        for target in TARGET_DATACENTERS:
            dc = by_code.get(target)
            if dc is None:
                log.warning("Datacenter %s no encontrado para %s", target, plan)
                continue

            available = dc.get("status") == "available" and dc.get("linuxStatus") == "available"
            key = f"{plan}:{target}"
            was_available = state.get(key, False)

            log.info(
                "%s / %s: status=%s linuxStatus=%s available=%s",
                plan_label,
                LOCATION_LABELS.get(target, target),
                dc.get("status"),
                dc.get("linuxStatus"),
                available,
            )

            if available and not was_available and not first_run:
                configurator = CONFIGURATOR_URL_TEMPLATE.format(plan=plan)
                msg = (
                    "🟢 STOCK DISPONIBLE\n"
                    f"Ubicacion: {LOCATION_LABELS.get(target, target)}\n"
                    f"Plan: {plan_label} ({plan})\n"
                    f"Configurador: {configurator}"
                )
                if telegram_send(msg):
                    state[key] = True
                    state_changed = True
            elif not available and was_available:
                state[key] = False
                state_changed = True
            elif key not in state:
                state[key] = available
                state_changed = True

    if first_run or FORCE_STARTUP_MESSAGE:
        lines = ["✅ Bot de stock OVH funcionando correctamente.", "Vigilando:"]
        for plan in PLAN_CODES:
            plan_label = PLAN_LABELS.get(plan, plan)
            locs = ", ".join(LOCATION_LABELS.get(t, t) for t in TARGET_DATACENTERS)
            lines.append(f"- {plan_label}: {locs}")
        telegram_send("\n".join(lines))
        if first_run:
            state["_initialized"] = True
        state_changed = True

    save_state(state)
    set_github_output("state_changed", "true" if state_changed else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
