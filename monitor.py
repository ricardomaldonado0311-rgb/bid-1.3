"""
Bid Searcher — solo GPO Small Purchase (publish.gpo.gov).

Cada corrida:
  1. Lee la lista oficial de oportunidades abiertas de GPO.
  2. Avisa de TODOS los bids nuevos (con o sin palabras clave; las que
     coinciden con keywords.txt salen marcadas con ★ y van primero).
  3. Vigila los bids de mis_bids.txt y avisa de cualquier cambio:
     nueva revisión, fecha de cotización, cantidad, entrega, el documento
     RFQ, o si desaparecen de la lista de abiertos.
  4. Guarda todo en state/gpo.json y escribe mis_bids_estado.md
     (tabla con el estado de tus bids, para verlo en GitHub).
"""
import difflib
import hashlib
import io
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "gpo.json"
MY_BIDS_FILE = ROOT / "mis_bids.txt"
STATUS_FILE = ROOT / "mis_bids_estado.md"
KEYWORDS_FILE = ROOT / "keywords.txt"

ET = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (oportunidades-bot)"}
GPO_API = "https://publish.gpo.gov/api"
GPO_LIST = "https://publish.gpo.gov/smallPurchase?activeTab=Opportunities"
MAX_PDF_PAGES = 40
KEEP_CLOSED_DAYS = 90   # los bids cerrados que no son tuyos se borran del estado

# Horas de revisión (hora del Este) y días (0=lunes ... 4=viernes).
# Deben coincidir con el cron de .github/workflows/monitor.yml.
RUN_HOURS_ET = {0, 16}
RUN_DAYS_ET = {0, 1, 2, 3, 4}

# Nombres en español de los campos de GPO que se comparan.
FIELD_LABELS = {
    "title": "Título",
    "fRevised": "Revisión",
    "quoteDueDate": "Cotizar antes de",
    "totalQuantity": "Cantidad",
    "shipDeliveryDate": "Entrega",
    "productType": "Tipo de producto",
    "datePosted": "Publicado",
}
# Campos que cambian sin que el bid cambie (no se avisan).
IGNORE_FIELDS = {"jobId"}


# ---------------------------------------------------------------- utilidades
def now_et():
    return datetime.now(ET)


def stamp():
    return now_et().strftime("%m/%d/%Y %I:%M %p ET")


def read_list(path):
    if not Path(path).exists():
        return []
    out = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def read_my_bids():
    """mis_bids.txt -> {jacket: nota}"""
    bids = {}
    for line in read_list(MY_BIDS_FILE):
        num, _, note = line.partition("|")
        m = re.search(r"\d{4,8}", num)
        if m:
            bids[m.group(0)] = note.strip()
    return bids


def normalize(text):
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[-_/]+", " ", text)
    return re.sub(r"\s+", " ", text)


def _word(w, flex):
    if flex and len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        base = w[:-2] if w.endswith("es") and len(w) > 4 else w[:-1]
        return re.escape(base) + r"(?:es|s)?"
    return re.escape(w)


def build_patterns(keywords):
    """"=palabra" se busca exacta (sin singular/plural)."""
    pats = []
    for kw in keywords:
        exact = kw.startswith("=")
        kw = kw.lstrip("=").strip()
        words = normalize(kw).split()
        mk = lambda flex, exact=exact, words=words: re.compile(
            r"(?<![a-z0-9])" + r"\s+".join(_word(w, flex and not exact) for w in words)
            + r"(?![a-z0-9])")
        pats.append((kw, mk(True), mk(False)))
    return pats


def find_matches(text, patterns, flex=True, ignore=()):
    norm = normalize(text or "")
    idx = 1 if flex else 2
    found = {p[0] for p in patterns if p[idx].search(norm)}
    found = {k for k in found if not normalize(k).startswith(ignore)} if ignore else found
    keys = {k: " " + normalize(k) + " " for k in found}
    return sorted(k for k in found
                  if not any(o != k and keys[k] in keys[o] for o in found))


def fmt_date(s):
    try:
        return datetime.fromisoformat(str(s)).strftime("%m/%d/%Y %I:%M %p")
    except Exception:
        return "" if s is None else str(s)


def fmt_field(key, value):
    if value in (None, ""):
        return "original" if key == "fRevised" else "(vacío)"
    if "date" in key.lower():
        return fmt_date(value)
    return str(value)


def rev_number(rev):
    m = re.search(r"\d+", str(rev or ""))
    return int(m.group(0)) if m else 0


# ------------------------------------------------------------- ¿toca correr?
def should_run():
    """El cron programa horas UTC de verano e invierno; aquí se decide cuál
    corresponde según la hora real del Este."""
    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return True  # corrida manual
    try:
        cron_hour_utc = int(os.getenv("CRON_SCHEDULE", "").split()[1])
    except (IndexError, ValueError):
        return True
    et = now_et()
    offset_h = int(-et.utcoffset().total_seconds() // 3600)  # 4 o 5
    wanted = {(h + offset_h) % 24 for h in RUN_HOURS_ET}
    return cron_hour_utc in wanted and et.weekday() in RUN_DAYS_ET


# ----------------------------------------------------------------- GPO: datos
def http_get(url, tries=3, **kw):
    kw.setdefault("headers", UA)
    kw.setdefault("timeout", 60)
    for n in range(tries):
        try:
            r = requests.get(url, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and n < tries - 1:
                time.sleep(10 * (n + 1))
                continue
            return r
        except requests.RequestException:
            if n == tries - 1:
                raise
            time.sleep(10 * (n + 1))


def fetch_open_jobs():
    """{jacket: job} con la revisión más reciente de cada jacket."""
    r = http_get(f"{GPO_API}/ContractorConnection/GetOpenJobs")
    r.raise_for_status()
    jobs = r.json().get("value")
    if jobs is None:
        raise RuntimeError("GPO respondió sin la lista de oportunidades")
    out = {}
    for j in jobs:
        jacket = str(j.get("jacketNumber", "")).strip()
        if not jacket:
            continue
        key = (rev_number(j.get("fRevised")), str(j.get("datePosted") or ""))
        old = out.get(jacket)
        if old is None or key > (rev_number(old.get("fRevised")),
                                 str(old.get("datePosted") or "")):
            out[jacket] = j
    return out


def fetch_rfq_text(job):
    """Texto del PDF del RFQ (o None si no se pudo leer)."""
    jacket = str(job.get("jacketNumber", "")).strip()
    job_id = str(job.get("jobId", ""))
    try:
        s = http_get(f"{GPO_API}/Files/GetRfqContainerSettings",
                     params={"containerName": "opportunities-and-pricing",
                             "blobName": f"{job_id}/{job_id}_{jacket}.pdf"}).json()
        pdf = http_get(s["value"]["blobSasUrl"], timeout=90)
        pdf.raise_for_status()
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf.content))
        return "\n".join((p.extract_text() or "")
                         for p in reader.pages[:MAX_PDF_PAGES])
    except Exception as e:
        print(f"  (no se pudo leer el RFQ de {jacket}: {e})")
        return None


def pdf_lines(text):
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in (text or "").splitlines())
    return [ln for ln in lines if ln]


def text_hash(lines):
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()[:16]


def scalar_fields(job):
    return {k: v for k, v in job.items()
            if isinstance(v, (str, int, float, bool)) or v is None}


def job_link(job):
    return (f"https://publish.gpo.gov/attachments/rfq/"
            f"{job.get('jobId', '')}/{job.get('jacketNumber', '')}")


def job_summary(job):
    return [
        ("Revisión", job.get("fRevised") or "original"),
        ("Tipo", job.get("productType", "")),
        ("Cantidad", job.get("totalQuantity", "")),
        ("Cotizar antes de", (fmt_date(job.get("quoteDueDate")) + " ET")
         if job.get("quoteDueDate") else ""),
        ("Entrega", fmt_date(job.get("shipDeliveryDate"))[:10]),
        ("Publicado", fmt_date(job.get("datePosted"))),
    ]


# ------------------------------------------------------------ comparar cambios
def field_changes(old_fields, new_fields):
    out = []
    for k in sorted(set(old_fields) | set(new_fields)):
        if k in IGNORE_FIELDS:
            continue
        a, b = old_fields.get(k), new_fields.get(k)
        if str(a) != str(b):
            label = FIELD_LABELS.get(k, k)
            out.append(f"{label}: {fmt_field(k, a)} → {fmt_field(k, b)}")
    # la revisión primero, luego la fecha de cotización
    order = {"Revisión": 0, "Cotizar antes de": 1}
    return sorted(out, key=lambda x: order.get(x.split(":")[0], 9))


def doc_changes(old_lines, new_lines, limit=12):
    added, removed = [], []
    for d in difflib.ndiff(old_lines, new_lines):
        if d.startswith("+ "):
            added.append(d[2:])
        elif d.startswith("- "):
            removed.append(d[2:])
    return added[:limit], removed[:limit], len(added), len(removed)


# ------------------------------------------------------------------- correo
CARD = ("border:1px solid #ccc;border-radius:6px;padding:10px;margin:10px 0;"
        "font-family:Arial,sans-serif")
H3 = ("font-family:Arial,sans-serif;margin:24px 0 6px;"
      "border-bottom:2px solid #1a5276;color:#1a5276")


def job_card(job, extra_html="", star=None, note=""):
    jacket = job.get("jacketNumber", "")
    head = (f"{'★ ' if star else ''}<b>Jacket {escape(str(jacket))}</b> — "
            f"<b>{escape((job.get('title') or '').strip())}</b>")
    det = " · ".join(f"{escape(k)}: {escape(str(v))}"
                     for k, v in job_summary(job) if v)
    kw = (f"<br><small><b>Palabras clave:</b> {escape(', '.join(star))}</small>"
          if star else "")
    nt = f"<br><small><b>Tu nota:</b> {escape(note)}</small>" if note else ""
    return (f"<div style='{CARD}'>{head}<br><small style='color:#444'>{det}</small>"
            f"{kw}{nt}{extra_html}<br><a href='{escape(job_link(job))}'>Ver RFQ</a>"
            f"</div>")


def build_email(updates, new_jobs, started, first_run):
    out = [f"<p style='font-family:Arial,sans-serif'>GPO Small Purchase · "
           f"<small>{escape(stamp())}</small></p>"]
    if updates:
        out.append(f"<h3 style='{H3}'>🔄 Cambios en tus bids ({len(updates)})</h3>")
        for u in updates:
            lis = "".join(f"<li>{escape(c)}</li>" for c in u["changes"])
            doc = ""
            if u.get("doc"):
                added, removed, na, nr = u["doc"]
                doc = "<br><small><b>El documento RFQ cambió</b>"
                if added:
                    doc += (f" — líneas nuevas ({na}):</small><pre style='white-space:"
                            f"pre-wrap;font-size:12px;background:#eef8ee;padding:6px'>"
                            + escape("\n".join(added)) + "</pre>")
                if removed:
                    doc += (f"<small>Líneas quitadas ({nr}):</small><pre style='white-"
                            f"space:pre-wrap;font-size:12px;background:#fbeeee;"
                            f"padding:6px'>" + escape("\n".join(removed)) + "</pre>")
                if not added and not removed:
                    doc += "</small>"
            extra = f"<ul style='margin:6px 0'>{lis}</ul>{doc}" if lis or doc else ""
            out.append(job_card(u["job"], extra, note=u["note"]))
    if started:
        out.append(f"<h3 style='{H3}'>📌 Ahora sigo estos bids ({len(started)})</h3>")
        for s in started:
            if s["job"]:
                out.append(job_card(s["job"], note=s["note"]))
            else:
                out.append(f"<div style='{CARD}'><b>Jacket {escape(s['jacket'])}</b>"
                           f" — no aparece en la lista de abiertos de GPO. Revisa el "
                           f"número; si lo publican o reabren, te aviso.</div>")
    if new_jobs:
        title = ("🆕 Bids abiertos en GPO (primera revisión)" if first_run
                 else "🆕 Bids nuevos en GPO")
        out.append(f"<h3 style='{H3}'>{title} ({len(new_jobs)})</h3>")
        out.append("<p style='font-family:Arial,sans-serif'><small>★ = coincide con "
                   "tus palabras clave.</small></p>")
        for n in new_jobs:
            extra = ("<br><small style='color:#b9770e'><b>Revisión de un bid "
                     "anterior</b></small>" if n["revision"] else "")
            out.append(job_card(n["job"], extra, star=n["matches"],
                                note=n.get("note", "")))
    return "\n".join(out)


def send_email(subject, html):
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ["SMTP_USER"]
    to = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(user, os.environ["SMTP_PASSWORD"])
        s.sendmail(user, to, msg.as_string())


# ------------------------------------------------------ tabla de mis bids (.md)
def write_status(state, my_bids):
    rows = []
    for jacket, note in my_bids.items():
        rec = state["jobs"].get(jacket)
        tr = state["tracked"].get(jacket, {})
        if rec:
            f = rec["fields"]
            status = "Abierto" if rec.get("open") else "Ya no está abierto"
            rows.append([jacket, (f.get("title") or "").strip(),
                         f.get("fRevised") or "original",
                         fmt_date(f.get("quoteDueDate")), status,
                         tr.get("last_change") or "—", note])
        else:
            rows.append([jacket, "—", "—", "—", "No encontrado en GPO", "—", note])
    lines = ["# Mis bids de GPO — estado",
             "",
             f"Actualizado por el bot: {stamp()}. Para añadir o quitar bids, "
             "edita `mis_bids.txt`.",
             "",
             "| Jacket | Título | Revisión | Cotizar antes de (ET) | Estado | "
             "Último cambio | Nota |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c).replace("|", "/") for c in r) + " |")
    if not rows:
        lines.append("| — | Aún no hay bids en `mis_bids.txt` | | | | | |")
    STATUS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------- main
def main():
    sys.stdout.reconfigure(line_buffering=True)
    if not should_run():
        print("No toca revisar en esta hora (ajuste de horario de verano). Fin.")
        return 0

    state = (json.loads(STATE_FILE.read_text()) if STATE_FILE.exists()
             else {"jobs": {}, "tracked": {}})
    first_run = not state["jobs"]
    my_bids = read_my_bids()
    patterns = build_patterns(read_list(KEYWORDS_FILE))
    today = now_et().strftime("%m/%d/%Y")

    try:
        open_jobs = fetch_open_jobs()
    except Exception as e:
        print(f"ERROR leyendo GPO: {e}")
        if os.getenv("DRY_RUN") != "1":
            send_email("⚠️ El bot no pudo leer GPO",
                       f"<p>No se pudo leer la lista de GPO Small Purchase.</p>"
                       f"<p><small>{escape(str(e))}</small></p>"
                       f"<p>Se volverá a intentar en la próxima revisión.</p>")
        return 0
    print(f"GPO: {len(open_jobs)} bids abiertos · Mis bids: {len(my_bids)}")

    updates, new_jobs, started = [], [], []

    # 1) Bids abiertos ahora
    for jacket, job in open_jobs.items():
        fields = scalar_fields(job)
        rec = state["jobs"].get(jacket)
        mine = jacket in my_bids
        is_new = rec is None
        revised = (rec is not None and
                   rev_number(job.get("fRevised")) > rev_number(rec["fields"].get("fRevised")))

        text = None
        if is_new or revised or mine:
            text = fetch_rfq_text(job)

        if is_new or (revised and not mine):
            body = text or ""
            in_title = find_matches(job.get("title", ""), patterns)
            in_doc = find_matches(body, patterns, flex=False, ignore=("jacket",))
            new_jobs.append({"job": job, "revision": not is_new,
                             "matches": sorted(set(in_title) | set(in_doc)),
                             "note": my_bids.get(jacket, "")})

        if rec is None:
            rec = {"first_seen": today, "fields": fields}
            state["jobs"][jacket] = rec

        if mine and jacket in state["tracked"]:
            changes = field_changes(rec["fields"], fields)
            if not rec.get("open", True):
                changes.insert(0, "Volvió a aparecer en la lista de abiertos")
            doc = None
            if text is not None and rec.get("pdf_lines") is not None:
                new_l = pdf_lines(text)
                if text_hash(new_l) != rec.get("pdf_hash"):
                    doc = doc_changes(rec["pdf_lines"], new_l)
            if changes or doc:
                updates.append({"job": job, "changes": changes, "doc": doc,
                                "note": my_bids[jacket]})
                desc = changes[0] if changes else "Cambió el documento RFQ"
                state["tracked"][jacket]["last_change"] = f"{today}: {desc}"

        rec["fields"] = fields
        rec["open"] = True
        rec["last_seen"] = today
        if text is not None:
            lines = pdf_lines(text)
            rec["pdf_hash"] = text_hash(lines)
            rec["pdf_lines"] = lines if mine else None

    # 2) Mis bids que ya no están abiertos
    for jacket, note in my_bids.items():
        rec = state["jobs"].get(jacket)
        if jacket in open_jobs or rec is None:
            continue
        if rec.get("open") and jacket in state["tracked"]:
            fake = {**rec["fields"], "jacketNumber": jacket}
            updates.append({"job": fake, "note": note, "doc": None, "changes": [
                "Ya no aparece en la lista de abiertos de GPO (cerró o lo retiraron)"]})
            state["tracked"][jacket]["last_change"] = f"{today}: ya no está abierto"
        rec["open"] = False

    # 3) Bids que acabas de añadir a mis_bids.txt
    for jacket, note in my_bids.items():
        if jacket in state["tracked"]:
            continue
        state["tracked"][jacket] = {"since": today, "last_change": ""}
        started.append({"jacket": jacket, "note": note,
                        "job": open_jobs.get(jacket)})
    for jacket in list(state["tracked"]):
        if jacket not in my_bids:
            del state["tracked"][jacket]   # lo quitaste de la lista

    # 4) Cerrar los que ya no están y limpiar los viejos
    cutoff = (now_et() - timedelta(days=KEEP_CLOSED_DAYS)).strftime("%Y%m%d")
    for jacket, rec in list(state["jobs"].items()):
        if jacket not in open_jobs:
            rec["open"] = False
            if jacket not in my_bids:
                rec["pdf_lines"] = None
                seen = datetime.strptime(rec.get("last_seen", today), "%m/%d/%Y")
                if seen.strftime("%Y%m%d") < cutoff:
                    del state["jobs"][jacket]

    # Resumen en el registro
    for u in updates:
        print(f"CAMBIO {u['job'].get('jacketNumber')}: {u['changes']}"
              f"{' + documento' if u['doc'] else ''}")
    for s in started:
        print(f"SIGUIENDO {s['jacket']}: {'abierto' if s['job'] else 'no encontrado'}")
    new_jobs.sort(key=lambda n: (not n["matches"], n["job"].get("title", "")))
    for n in new_jobs:
        print(f"NUEVO {'★' if n['matches'] else ' '} {n['job'].get('jacketNumber')} "
              f"{n['job'].get('fRevised') or ''} {n['job'].get('title', '')[:70]}"
              f" {n['matches'] or ''}")

    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    write_status(state, my_bids)

    if (updates or new_jobs or started) and os.getenv("DRY_RUN") != "1":
        parts = []
        if updates:
            parts.append(f"{len(updates)} cambio(s) en tus bids")
        if new_jobs:
            parts.append(f"{len(new_jobs)} bid(s) nuevo(s)")
        if started:
            parts.append(f"siguiendo {len(started)} bid(s)")
        icon = "🔄" if updates else "🆕" if new_jobs else "📌"
        send_email(f"{icon} GPO: " + " · ".join(parts),
                   build_email(updates, new_jobs, started, first_run))
    print("Correo:", "sí" if (updates or new_jobs or started) else "nada nuevo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
