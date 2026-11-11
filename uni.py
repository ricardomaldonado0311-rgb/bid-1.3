"""
Bot "Uni and Others": revisa los portales Bonfire de uni_portales.txt
(universidades, distritos escolares, ciudades y otras agencias).

Cada corrida:
  1. Lee la lista de oportunidades abiertas de cada portal (API pública de Bonfire).
  2. Avisa por correo de TODAS las oportunidades nuevas. Las que coinciden con
     keywords.txt llevan ★ y van primero.
  3. Avisa (una sola vez) de los portales que dejan de responder.
  4. Guarda lo visto en state/uni.json.

Horario: 11 a. m. y 5 p. m. hora del Este, de lunes a viernes.
Usa el mismo correo, las mismas contraseñas y las mismas palabras clave que el
bot de GPO (monitor.py).
"""

import json
import os
import requests
import sys
import time
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlparse

from monitor import (ET, ROOT, UA, KEYWORDS_FILE, build_patterns, find_matches,
                     http_get, now_et, read_list, send_email)

PORTALS_FILE = ROOT / "uni_portales.txt"
STATE_FILE = ROOT / "state" / "uni.json"
BOT_NAME = "Uni and Others"

RUN_HOURS_ET = {11, 17}          # 11 a. m. y 5 p. m. hora del Este
RUN_DAYS_ET = {0, 1, 2, 3, 4}    # lunes a viernes
PAUSE = 1.0                      # segundos entre portales (para no saturar Bonfire)
FAIL_ALERT = 3                   # avisar si un portal falla N corridas seguidas
TIME_BUDGET = 20 * 60            # máximo de segundos por corrida; lo que falte
                                 # se revisa primero en la siguiente corrida
SESSION = requests.Session()


# ------------------------------------------------------------- ¿toca correr?
def should_run():
    """Cada hora del cron está en su propia línea (verano e invierno); aquí se
    decide cuál corresponde a la hora del Este de hoy."""
    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return True  # corrida manual
    et = now_et()
    if et.weekday() not in RUN_DAYS_ET:
        return False
    try:
        cron_hour_utc = int(os.getenv("CRON_SCHEDULE", "").split()[1])
    except (IndexError, ValueError):
        return True
    offset_h = int(-et.utcoffset().total_seconds() // 3600)  # 4 o 5
    return cron_hour_utc in {(h + offset_h) % 24 for h in RUN_HOURS_ET}


# -------------------------------------------------------------- portales
def read_portals():
    hosts = []
    for line in read_list(PORTALS_FILE):
        h = line.split()[0].lower() if line.split() else ""
        h = urlparse(h if "://" in h else "https://" + h).netloc
        if h and h not in hosts:
            hosts.append(h)
    return hosts


def to_et(s):
    """'2026-11-06 19:00:00' (UTC) -> '11/06/2026 02:00 PM ET'."""
    try:
        d = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return d.astimezone(ET).strftime("%m/%d/%Y %I:%M %p ET")
    except Exception:
        return s or ""


class Blocked(Exception):
    pass


def fetch_portal(host):
    """Lista de oportunidades abiertas de un portal Bonfire."""
    # Un solo intento y espera corta: si Bonfire tarda o pide esperar, el portal
    # se reintenta al final de la corrida (así una corrida no se queda colgada).
    r = SESSION.get(f"https://{host}/PublicPortal/getOpenPublicOpportunitiesSectionData",
                    headers={**UA, "Accept": "application/json",
                             "X-Requested-With": "XMLHttpRequest"},
                    timeout=(10, 25))
    if r.status_code in (403, 429, 503):
        raise Blocked(f"HTTP {r.status_code}")
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("no tiene portal público")
    payload = data.get("payload") or {}
    projects = payload.get("projects") or {}
    if isinstance(projects, dict):
        projects = list(projects.values())
    depts = payload.get("departments") or {}
    items = []
    for pj in projects:
        pid = str(pj.get("ProjectID", ""))
        if not pid:
            continue
        dep = depts.get(str(pj.get("DepartmentID"))) if isinstance(depts, dict) else None
        items.append({
            "id": pid,
            "ref": (pj.get("ReferenceID") or "").strip(),
            "title": (pj.get("ProjectName") or "").strip(),
            "dept": dep.get("DepartmentName", "") if isinstance(dep, dict) else "",
            "close": to_et(pj.get("DateClose", "")),
            "link": f"https://{host}/opportunities/{pid}",
        })
    return items


# ------------------------------------------------------------------ correo
def card(o):
    star = "★ " if o["matches"] else ""
    head = (f"<b>{escape(o['ref'])}</b> — " if o["ref"] else "") + f"<b>{escape(o['title'])}</b>"
    det = " · ".join(escape(x) for x in [o["portal"], o["dept"],
                                          f"Cierra: {o['close']}" if o["close"] else ""] if x)
    kw = (f"<br><small>Palabras: {escape(', '.join(o['matches']))}</small>"
          if o["matches"] else "")
    return ("<div style='border:1px solid #ccc;border-radius:6px;padding:8px 10px;"
            "margin:8px 0;font-family:Arial,sans-serif'>"
            f"{star}{head}<br><small style='color:#444'>{det}</small>{kw}<br>"
            f"<a href='{escape(o['link'])}'>Ver oportunidad</a></div>")


def build_email(new, dead, first_run, total_open, n_portals):
    when = now_et().strftime("%m/%d/%Y %I:%M %p ET")
    out = [f"<p style='font-family:Arial,sans-serif'><b>{BOT_NAME}</b> · {when}<br>"
           f"<small>{n_portals} portales revisados · {total_open} oportunidades abiertas</small></p>"]
    stars = [o for o in new if o["matches"]]
    rest = [o for o in new if not o["matches"]]
    if first_run:
        out.append("<p style='font-family:Arial,sans-serif'>Primera revisión: estas son las "
                   "oportunidades <b>abiertas hoy</b> que coinciden con tus palabras clave. "
                   "Desde la próxima revisión te aviso de todas las nuevas.</p>")
    if stars:
        out.append("<h3 style='font-family:Arial,sans-serif;border-bottom:2px solid #1a5276'>"
                   f"★ Coinciden con tus palabras clave ({len(stars)})</h3>")
        out += [card(o) for o in stars]
    if rest:
        out.append("<h3 style='font-family:Arial,sans-serif;border-bottom:2px solid #1a5276'>"
                   f"🆕 Otras oportunidades nuevas ({len(rest)})</h3>")
        out += [card(o) for o in rest]
    if dead:
        out.append("<h3 style='font-family:Arial,sans-serif;border-bottom:2px solid #b03a2e'>"
                   f"⚠️ Portales que no responden ({len(dead)})</h3>"
                   "<p style='font-family:Arial,sans-serif'><small>Puede que el link esté mal "
                   "o que la agencia ya no use Bonfire. Revísalos y corrígelos o bórralos de "
                   "uni_portales.txt.</small></p><ul style='font-family:Arial,sans-serif'>"
                   + "".join(f"<li>{escape(h)} — {escape(why)}</li>" for h, why in dead)
                   + "</ul>")
    return "\n".join(out)


# -------------------------------------------------------------------- main
def main():
    if not should_run():
        print("No toca revisar en esta hora. Fin.")
        return 0

    portals = read_portals()
    patterns = build_patterns(read_list(KEYWORDS_FILE))
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("portals", {})
    first_run = not state["portals"]
    today = now_et().strftime("%m/%d/%Y")
    print(f"{BOT_NAME}: {len(portals)} portales")

    results, errors, retry = {}, {}, []
    t_start = time.time()

    def try_portal(host):
        t0 = time.time()
        try:
            results[host] = fetch_portal(host)
            errors.pop(host, None)
            print(f"  {host}: {len(results[host])} abiertas ({time.time() - t0:.1f}s)")
        except (Blocked, requests.Timeout, requests.ConnectionError) as e:
            errors[host] = str(e)[:120]
            print(f"  {host}: espera/bloqueo ({time.time() - t0:.1f}s) {errors[host]}")
            return False
        except Exception as e:
            errors[host] = str(e)[:120]
            print(f"  {host}: error ({time.time() - t0:.1f}s) {errors[host]}")
        return True

    # Primero los que hace más tiempo no se revisan (o nunca).
    order = sorted(portals, key=lambda h: state["portals"].get(h, {}).get("ts", 0))
    skipped = []
    for host in order:
        if time.time() - t_start > TIME_BUDGET:
            skipped.append(host)
            continue
        if not try_portal(host):
            retry.append(host)
        time.sleep(PAUSE)
    if retry and time.time() - t_start < TIME_BUDGET:
        print(f"Reintentando {len(retry)} portales que tardaron o pidieron esperar…")
        time.sleep(60)
        for host in retry:
            if time.time() - t_start > TIME_BUDGET + 5 * 60:
                break
            try_portal(host)
            time.sleep(PAUSE * 3)
    if skipped:
        print(f"Sin tiempo para {len(skipped)} portales; van primero en la próxima corrida.")

    new, dead, total_open = [], [], 0
    for host in portals:
        if host in skipped:
            continue
        rec = state["portals"].setdefault(host, {"ids": {}, "fails": 0})
        rec["ts"] = int(time.time())
        portal_first = not rec["ids"] and rec.get("checked") is None
        if host in errors:
            rec["fails"] = rec.get("fails", 0) + 1
            print(f"ERROR {host}: {errors[host]}")
            if not rec.get("reported") and (rec["fails"] >= FAIL_ALERT or first_run):
                dead.append((host, errors[host]))
                rec["reported"] = True
            continue
        rec["fails"] = 0
        rec.pop("reported", None)
        rec["checked"] = today
        items = results[host]
        total_open += len(items)
        ids = {}
        for it in items:
            ids[it["id"]] = rec["ids"].get(it["id"], today)
            if it["id"] in rec["ids"]:
                continue
            matches = find_matches(it["title"] + " ; " + it["dept"], patterns)
            if portal_first and not matches:
                continue  # portal recién añadido: solo lo que coincide
            new.append({**it, "portal": host.split(".")[0], "matches": matches})
        rec["ids"] = ids
    for host in list(state["portals"]):
        if host not in portals:
            del state["portals"][host]  # lo quitaste de la lista

    new.sort(key=lambda o: (not o["matches"], o["portal"], o["title"]))
    tried = len(portals) - len(skipped)
    ok = tried - len(errors)
    print(f"Portales leídos: {ok} de {tried} · abiertas: {total_open} · "
          f"nuevas: {len(new)} ({sum(1 for o in new if o['matches'])} ★) · "
          f"{time.time() - t_start:.0f}s")
    for o in new:
        print(f"NUEVO {'★' if o['matches'] else ' '} {o['portal']}: {o['title'][:70]} "
              f"{o['matches'] or ''}")

    # Si casi todo falló, es un problema de Bonfire o de la conexión: no tocar el estado.
    if tried and ok < tried / 2:
        print("Más de la mitad de los portales falló; no se guarda el estado.")
        if os.getenv("DRY_RUN") != "1":
            send_email(f"⚠️ {BOT_NAME}: no pude leer Bonfire",
                       f"<p>Solo se pudieron leer {ok} de {tried} portales. "
                       "Puede ser un bloqueo temporal de Bonfire; se vuelve a intentar "
                       "en la próxima revisión.</p>")
        return 0

    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))

    if (new or dead) and os.getenv("DRY_RUN") != "1":
        n_star = sum(1 for o in new if o["matches"])
        if first_run:
            subject = f"🎓 {BOT_NAME}: primera revisión · {n_star} ★ abiertas"
        elif new:
            subject = f"🎓 {BOT_NAME}: {len(new)} nueva(s)" + (f" · {n_star} ★" if n_star else "")
        else:
            subject = f"⚠️ {BOT_NAME}: portales que no responden"
        send_email(subject, build_email(new, dead, first_run, total_open, len(portals)))
        print("Correo: sí")
    else:
        print("Correo: nada nuevo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
