"""
Bot de monitoreo de oportunidades por palabras clave.

Fuentes:
  * sites.txt: una URL por línea. Cada sitio conocido tiene su lector
    (GPO, Columbus, Philadelphia, BuySpeed, Cleveland, Gwinnett, Louisiana,
    Carolina del Sur). Cualquier otra URL se abre con un navegador real
    (Playwright) y se leen las filas/tarjetas que muestra.
  * bonfire_sites.txt: portales Bonfire (uno por línea), leídos con su API
    pública.
Busca las palabras de keywords.txt y manda un correo SOLO con lo nuevo que
coincida. Lo ya revisado se guarda en state/seen.json.
"""

import hashlib
import io
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen.json"
DEBUG_DIR = ROOT / "debug"
ET = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (oportunidades-bot)"}
BONFIRE_FILE = ROOT / "bonfire_sites.txt"
BONFIRE_PAUSE = 1.5   # segundos entre portales Bonfire (para no saturar)
MAX_PDF_PAGES = 30    # solo se leen las primeras páginas de cada PDF

# Horas de revisión (hora del Este) y días (0=lunes ... 4=viernes)
RUN_HOURS_ET = {0, 16}
RUN_DAYS_ET = {0, 1, 2, 3, 4}


# ---------------------------------------------------------------- utilidades
def read_list(path):
    out = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def normalize(text):
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[-_/]+", " ", text)
    return re.sub(r"\s+", " ", text)


def _word(w, flex):
    """flex=True acepta singular o plural (bags/bag, boxes/box)."""
    if flex and len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        base = w[:-2] if w.endswith("es") and len(w) > 4 else w[:-1]
        return re.escape(base) + r"(?:es|s)?"
    return re.escape(w)


def build_patterns(keywords):
    pats = []
    for kw in keywords:
        words = normalize(kw).split()
        mk = lambda flex: re.compile(
            r"(?<![a-z0-9])" + r"\s+".join(_word(w, flex) for w in words)
            + r"(?![a-z0-9])")
        pats.append((kw, mk(True), mk(False)))
    return pats


def find_matches(text, patterns, flex=True):
    norm = normalize(text or "")
    idx = 1 if flex else 2
    found = {p[0] for p in patterns if p[idx].search(norm)}
    # si coincide "tote bags", no repetir también "bags"
    keys = {k: " " + normalize(k) + " " for k in found}
    return sorted(k for k in found
                  if not any(o != k and keys[k] in keys[o] for o in found))


def short_hash(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


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
    now_et = datetime.now(ET)
    offset_h = int(-now_et.utcoffset().total_seconds() // 3600)  # 4 o 5
    wanted = {(h + offset_h) % 24 for h in RUN_HOURS_ET}
    return cron_hour_utc in wanted and now_et.weekday() in RUN_DAYS_ET


# ------------------------------------------------------------------ PDF text
def pdf_text(data, max_pages=MAX_PDF_PAGES):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])


def http_get(url, tries=3, **kw):
    """GET con reintentos suaves (espera más en cada intento)."""
    kw.setdefault("headers", UA)
    kw.setdefault("timeout", 60)
    for n in range(tries):
        try:
            r = requests.get(url, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and n < tries - 1:
                time.sleep(5 * (n + 1))
                continue
            return r
        except requests.RequestException:
            if n == tries - 1:
                raise
            time.sleep(5 * (n + 1))


def pdf_from_url(url):
    r = http_get(url, timeout=90)
    r.raise_for_status()
    if len(r.content) > 25_000_000:
        return ""
    return pdf_text(r.content)


def to_et(dt_utc_str, fmt="%Y-%m-%d %H:%M:%S"):
    """'2026-11-06 19:00:00' (UTC) -> '11/06/2026 02:00 PM ET'."""
    try:
        d = datetime.strptime(dt_utc_str, fmt).replace(tzinfo=timezone.utc)
        return d.astimezone(ET).strftime("%m/%d/%Y %I:%M %p ET")
    except Exception:
        return dt_utc_str or ""


# ------------------------------------------------------------ fuente: GPO
GPO_API = "https://publish.gpo.gov/api"


def fmt_date(s):
    try:
        return datetime.fromisoformat(s).strftime("%m/%d/%Y %I:%M %p")
    except Exception:
        return s or ""


def gpo_small_purchase(site, seen_ids, patterns):
    r = requests.get(f"{GPO_API}/ContractorConnection/GetOpenJobs",
                     headers=UA, timeout=60)
    r.raise_for_status()
    jobs = r.json().get("value") or []
    items = []
    for j in jobs:
        jacket = str(j.get("jacketNumber", "")).strip()
        rev = str(j.get("fRevised", "")).strip()
        job_id = str(j.get("jobId", ""))
        item = {
            "id": short_hash("gpo", jacket, rev),
            "ident": f"Jacket {jacket} {rev}".strip(),
            "title": (j.get("title") or "").strip(),
            "details": [
                ("Tipo", j.get("productType", "")),
                ("Cantidad", j.get("totalQuantity", "")),
                ("Cotizar antes de", fmt_date(j.get("quoteDueDate")) + " ET"),
                ("Publicado", fmt_date(j.get("datePosted"))),
                ("Entrega", fmt_date(j.get("shipDeliveryDate"))[:10]),
            ],
            "link": f"https://publish.gpo.gov/attachments/rfq/{job_id}/{jacket}",
            "body": "",
            # en GPO "jacket" es el número de trabajo, no una chaqueta
            "body_ignore": ("jacket",),
        }
        # El PDF del RFQ solo se descarga para oportunidades nuevas
        if item["id"] not in seen_ids:
            try:
                blob = f"{job_id}/{job_id}_{jacket}.pdf"
                s = requests.get(
                    f"{GPO_API}/Files/GetRfqContainerSettings",
                    params={"containerName": "opportunities-and-pricing",
                            "blobName": blob},
                    headers=UA, timeout=60).json()["value"]
                pdf = requests.get(s["blobSasUrl"], headers=UA, timeout=90)
                pdf.raise_for_status()
                item["body"] = pdf_text(pdf.content)
            except Exception as e:
                print(f"  (no se pudo leer el RFQ de {jacket}: {e})")
        items.append(item)
    return items


# ---------------------------------------------- fuente: cualquier otra página
ROW_JS = """
() => {
  const sel = 'tr, [role=row], mat-row, .card, li, article';
  const out = [];
  document.querySelectorAll(sel).forEach(el => {
    const t = (el.innerText || '').trim();
    if (t.length > 25 && t.length < 4000) out.push(t);
  });
  return out;
}
"""


def generic_page(site, seen_ids, patterns):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(site, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(4_000)
        rows = list(dict.fromkeys(page.evaluate(ROW_JS)))
        rows = [r for r in rows if not any(r != o and o in r for o in rows)]
        if not rows:  # sin filas: usar el texto completo por párrafos
            rows = [b.strip() for b in page.inner_text("body").split("\n\n")
                    if len(b.strip()) > 25]
        DEBUG_DIR.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", site.lower())[:60]
        page.screenshot(path=str(DEBUG_DIR / f"{slug}.png"), full_page=True)
        browser.close()
    return [{
        "id": short_hash(site, normalize(r)),
        "ident": "",
        "title": r.splitlines()[0][:200],
        "details": [],
        "link": site,
        "body": r,
    } for r in rows]


# ------------------------------------------- fuente: City of Columbus (Ohio)
def columbus_vendor_services(site, seen_ids, patterns):
    base = "https://columbusvendorservices.powerappsportals.com"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines_rel = "cr820_RFQLine_RFQHeader_cr820_RFQHeader"
    params = {
        "$select": ("cr820_rfqcaseid,cr820_documenttitle,cr820_requestingdepartment,"
                    "cr820_solicitationname,cr820_expirydateandtime,"
                    "cr820_psnrfqdescription"),
        "$expand": f"{lines_rel}($select=cr820_productname,cr820_procurementcategory)",
        "$filter": f"(cr820_expirydateandtime ge {now})",
    }
    r = requests.get(f"{base}/_api/cr820_rfqheaders", params=params,
                     headers={**UA, "Accept": "application/json"}, timeout=60)
    r.raise_for_status()
    items = []
    for v in r.json().get("value", []):
        case = v.get("cr820_rfqcaseid", "")
        lines = [f"{l.get('cr820_productname') or ''} ({l.get('cr820_procurementcategory') or ''})"
                 for l in v.get(lines_rel) or []]
        due = v.get("cr820_expirydateandtime") or ""
        try:
            due = datetime.fromisoformat(due.replace("Z", "+00:00")) \
                .astimezone(ET).strftime("%m/%d/%Y %I:%M %p ET")
        except Exception:
            pass
        items.append({
            "id": short_hash("columbus", case),
            "ident": case,
            # el título incluye los productos de cada línea del RFQ
            "title": (v.get("cr820_documenttitle") or "").strip(),
            "title_extra": " ; ".join(lines),
            "details": [
                ("Departamento", v.get("cr820_requestingdepartment", "")),
                ("Tipo", v.get("cr820_solicitationname", "")),
                ("Cierra", due),
                ("Productos", "; ".join(lines)[:300]),
            ],
            "link": f"{base}/OpenRFQs/",
            "body": v.get("cr820_psnrfqdescription") or "",
        })
    return items


# --------------------------------------- fuente: Philadelphia (PHLContracts)
def _html_to_text(h):
    h = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    from html import unescape
    return re.sub(r"\s+", " ", unescape(h)).strip()


def _field(text, label, nxt):
    m = re.search(re.escape(label) + r"\s*(.*?)\s*" + re.escape(nxt), text)
    return m.group(1).strip() if m else ""


def phl_contracts(site, seen_ids, patterns):
    from playwright.sync_api import sync_playwright
    base = "https://www.phlcontracts.phila.gov"
    ids = []
    # 1) Lista de licitaciones abiertas (estado "Sent"): requiere navegador
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{base}/bso/view/search/external/advancedSearchBid.xhtml",
                  wait_until="networkidle", timeout=90_000)
        page.select_option('select[name="bidSearchForm:status"]', "2BS")
        page.get_by_role("button", name="Search", exact=True).click()
        page.wait_for_selector('a[href*="bidDetail.sda"]', timeout=60_000)
        for _ in range(60):  # hasta 60 páginas de 25
            page_ids = page.eval_on_selector_all(
                'a[href*="bidDetail.sda"]', "els => els.map(e => e.innerText.trim())")
            ids.extend(i for i in page_ids if i and i not in ids)
            nxt = page.query_selector("a.ui-paginator-next")
            if not nxt or "ui-state-disabled" in (nxt.get_attribute("class") or ""):
                break
            first = page_ids[0] if page_ids else ""
            nxt.click()
            page.wait_for_function(
                "f => { const a = document.querySelector('a[href*=\"bidDetail.sda\"]');"
                " return a && a.innerText.trim() !== f; }", arg=first, timeout=30_000)
        browser.close()

    # 2) Detalle de cada licitación nueva (página pública)
    items = []
    for bid in ids:
        item_id = short_hash("phl", bid)
        link = f"{base}/bso/external/bidDetail.sda?docId={bid}&external=true&parentUrl=close"
        item = {"id": item_id, "ident": bid, "title": "", "details": [],
                "link": link, "body": ""}
        if item_id not in seen_ids:
            try:
                r = requests.get(link, headers=UA, timeout=60)
                r.raise_for_status()
                t = _html_to_text(r.text)
                item["title"] = _field(t, "Description:", "Bid Opening Date:")
                item["details"] = [
                    ("Departamento", _field(t, "Department:", "Location:")),
                    ("Tipo", _field(t, "Type Code:", "Allow Electronic Quote:")),
                    ("Cierra", _field(t, "Bid Opening Date:", "Purchaser:")),
                ]
                k = t.find("Item # 1")
                body = t[k:].split("Copyright")[0] if k >= 0 else ""
                # quitar textos de categoría NIGP (generan falsos avisos)
                item["body"] = re.sub(r"NIGP Code:.*?(?=Qty Unit Cost)", " ", body)
            except Exception as e:
                item["error"] = True  # se reintenta en la próxima revisión
                print(f"  (no se pudo leer el detalle de {bid}: {e})")
        items.append(item)
    return items


# ----------------------------------------- fuente: portales Bonfire (Euna)
def bonfire_portal(site, seen_ids, patterns, first=False):
    """API pública de cada portal: lista de oportunidades abiertas."""
    host = urlparse(site if "://" in site else "https://" + site).netloc
    r = http_get(f"https://{host}/PublicPortal/getOpenPublicOpportunitiesSectionData",
                 headers={**UA, "Accept": "application/json",
                          "X-Requested-With": "XMLHttpRequest"})
    if r.status_code in (403, 429, 503):
        raise RuntimeError(f"bloqueado (HTTP {r.status_code})")
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        # el portal existe pero no publica nada en público (p. ej. Tomball)
        print(f"  ({host}: sin portal público)")
        return []
    payload = data.get("payload") or {}
    projects = payload.get("projects") or {}
    if isinstance(projects, dict):
        projects = list(projects.values())
    depts = payload.get("departments") or {}
    name = host.split(".")[0]
    items = []
    for pj in projects:
        pid = str(pj.get("ProjectID", ""))
        dep = depts.get(str(pj.get("DepartmentID"))) if isinstance(depts, dict) else None
        dep_name = dep.get("DepartmentName", "") if isinstance(dep, dict) else ""
        items.append({
            "id": short_hash("bonfire", host, pid),
            "ident": pj.get("ReferenceID") or "",
            "title": (pj.get("ProjectName") or "").strip(),
            "details": [("Portal", name), ("Departamento", dep_name),
                        ("Cierra", to_et(pj.get("DateClose", "")))],
            "link": f"https://{host}/opportunities/{pid}",
            "body": "",
        })
    return items


# ------------------- fuente: sitios BuySpeed (Illinois, Islas Vírgenes, NJ)
ROWS_JS = """
rows => rows.map(r => {
  const a = r.querySelector('a[href*="bidDetail.sda"]');
  const cells = [...r.querySelectorAll('td')]
      .map(td => td.textContent.replace(/\\s+/g, ' ').trim());
  const get = l => { const c = cells.find(x => x.startsWith(l));
                     return c ? c.slice(l.length).trim() : ''; };
  return {href: a ? a.getAttribute('href') : '', desc: get('Description'),
          status: get('Status'), org: get('Organization Name'),
          date: get('Bid Opening Date')};
})
"""


def buyspeed(site, seen_ids, patterns, first=False):
    from playwright.sync_api import sync_playwright
    u = urlparse(site)
    base = f"{u.scheme}://{u.netloc}"
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{base}/bso/view/search/external/advancedSearchBid.xhtml?openBids=true",
                  wait_until="networkidle", timeout=90_000)
        page.wait_for_selector('a[href*="bidDetail.sda"]', timeout=60_000)
        for _ in range(60):  # hasta 60 páginas de 25
            page_rows = page.eval_on_selector_all("tr[data-ri]", ROWS_JS)
            rows.extend(x for x in page_rows if x["href"])
            nxt = page.query_selector("a.ui-paginator-next")
            if not nxt or "ui-state-disabled" in (nxt.get_attribute("class") or ""):
                break
            first_href = page_rows[0]["href"] if page_rows else ""
            nxt.click()
            page.wait_for_function(
                "f => { const a = document.querySelector('a[href*=\"bidDetail.sda\"]');"
                " return a && a.getAttribute('href') !== f; }",
                arg=first_href, timeout=30_000)
        browser.close()

    items, done = [], set()
    for row in rows:
        # solo las abiertas ("Sent"); Illinois también lista adjudicadas
        if row["status"] and "sent" not in row["status"].lower():
            continue
        bid = (parse_qs(urlparse(row["href"]).query).get("docId") or [""])[0]
        if not bid or bid in done:
            continue
        done.add(bid)
        item_id = short_hash("buyspeed", u.netloc, bid)
        link = f"{base}/bso/external/bidDetail.sda?docId={bid}&external=true&parentUrl=close"
        item = {"id": item_id, "ident": bid, "title": row["desc"],
                "details": [("Organización", row["org"]), ("Cierra", row["date"])],
                "link": link, "body": ""}
        # el detalle (artículos) solo para las nuevas, y no en la 1.ª revisión
        if item_id not in seen_ids and not first:
            try:
                r = http_get(link)
                r.raise_for_status()
                t = _html_to_text(r.text)
                item["title"] = _field(t, "Description:", "Bid Opening Date:") or row["desc"]
                dep = _field(t, "Department:", "Location:")
                if dep:
                    item["details"].insert(0, ("Departamento", dep))
                k = t.find("Item # 1")
                body = t[k:].split("Copyright")[0] if k >= 0 else ""
                item["body"] = re.sub(r"NIGP Code:.*?(?=Qty Unit Cost)", " ", body)
                time.sleep(0.5)
            except Exception as e:
                item["error"] = True  # se reintenta en la próxima revisión
                print(f"  (no se pudo leer el detalle de {bid}: {e})")
        items.append(item)
    return items


# ------------------------------------------- fuente: City of Cleveland (Ohio)
def cleveland(site, seen_ids, patterns, first=False):
    from bs4 import BeautifulSoup
    base = "https://www.clevelandohio.gov"
    r = http_get(site)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for div in soup.select("div.accordion-item"):
        head = div.select_one("span.h3")
        if not head:
            continue
        title = head.get_text(" ", strip=True)
        m = re.match(r"^(\d+(?:-\d+)?)\s+-\s+(.*)$", title)
        ident, title = (m.group(1), m.group(2)) if m else ("", title)
        body_el = div.select_one(".accordion-body")
        text = body_el.get_text(" ", strip=True) if body_el else ""
        pdf = div.select_one('a[href$=".pdf"]')
        pdf_url = (base + pdf["href"]) if pdf and pdf["href"].startswith("/") else (
            pdf["href"] if pdf else "")
        closing = re.search(r"(?:Closing Date|Sealed Bids will be opened at Noon on):?\s*([^:]*?\d{4}(?:,\s*[\d:]+\s*[AP]M)?)", text)
        item = {
            "id": short_hash("cleveland", ident, title),
            "ident": ident,
            "title": title,
            "details": [("Ciudad", "Cleveland, OH"),
                        ("Cierra", closing.group(1).strip() if closing else "")],
            "link": pdf_url or site,
            "body": text,
        }
        if pdf_url and item["id"] not in seen_ids and not first:
            try:
                item["body"] += "\n" + pdf_from_url(pdf_url)
            except Exception as e:
                print(f"  (no se pudo leer el PDF de {title[:40]}: {e})")
        items.append(item)
    return items


# --------------------------------------------- fuente: Gwinnett County (GA)
def gwinnett(site, seen_ids, patterns, first=False):
    from bs4 import BeautifulSoup
    base = "https://www.gwinnettcounty.com"
    r = http_get(site)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for li in soup.select("li.list-group-item"):
        a = li.select_one('strong a[href*="/static/upload/bid/"]')
        if not a:
            continue
        ps = [p.get_text(" ", strip=True) for p in li.select("p")]
        title = ps[1] if len(ps) > 1 else ""
        opening = next((x.split(":", 1)[1].strip() for x in ps
                        if x.startswith("Opening Date")), "")
        pdf_url = base + a["href"] if a["href"].startswith("/") else a["href"]
        ident = a.get_text(strip=True)
        item = {
            "id": short_hash("gwinnett", ident),
            "ident": ident,
            "title": title,
            "details": [("Condado", "Gwinnett County, GA"), ("Apertura", opening)],
            "link": pdf_url,
            "body": "",
        }
        if item["id"] not in seen_ids and not first:
            try:
                item["body"] = pdf_from_url(pdf_url)
            except Exception as e:
                print(f"  (no se pudo leer el PDF de {ident}: {e})")
        items.append(item)
    return items


# ------------------------------------------------- fuente: Louisiana (LaPAC)
def louisiana(site, seen_ids, patterns, first=False):
    from bs4 import BeautifulSoup
    base = "https://wwwcfprd.doa.louisiana.gov"
    r = http_get(f"{base}/osp/lapac/catbids.cfm")
    r.raise_for_status()
    cats = re.findall(r'href="(?:https?://wwwcfprd\.doa\.louisiana\.gov)?'
                      r'(/osp/lapac/dspBid\.cfm\?search=segment&(?:amp;)?term=\d+)"', r.text)
    cats = [c.replace("&amp;", "&") for c in cats]
    items, done = [], set()
    for path in dict.fromkeys(cats):
        rc = http_get(base + path.replace("&amp;", "&"))
        if rc.status_code != 200:
            continue
        soup = BeautifulSoup(rc.text, "html.parser")
        for tr in soup.select("table.bid tr"):
            first_td = tr.find("td")
            if not first_td or not first_td.get("rowspan"):
                continue  # filas de addendum
            bid = first_td.get_text(" ", strip=True)
            if not bid or bid in done:
                continue
            done.add(bid)
            tds = tr.find_all("td")
            desc_td = tr.select_one("td.txt")
            title = ""
            pdf_url = ""
            if desc_td:
                title = desc_td.get_text(" ", strip=True).split("Original:")[0].strip()
                a = desc_td.find("a", href=True)
                if a:
                    pdf_url = base + a["href"] if a["href"].startswith("/") else a["href"]
            opens = tds[3].get_text(" ", strip=True) if len(tds) > 3 else ""
            item = {
                "id": short_hash("lapac", bid),
                "ident": bid,
                "title": title,
                "details": [("Estado", "Louisiana"), ("Apertura", opens)],
                "link": pdf_url or base + path.replace("&amp;", "&"),
                "body": "",
            }
            if pdf_url and item["id"] not in seen_ids and not first:
                try:
                    item["body"] = pdf_from_url(pdf_url)
                except Exception as e:
                    print(f"  (no se pudo leer el PDF de {bid}: {e})")
            items.append(item)
        time.sleep(0.5)
    return items


# ------------------------------------- fuente: South Carolina (SCBO diario)
SCBO_CATS = {2: "Architectural/Engineering", 3: "Construction",
             4: "Consultant/Professional", 5: "Environmental Remediation",
             6: "Equipment", 7: "Information Technology", 8: "Maintenance/Repair",
             9: "Minor Construction", 10: "Printing", 11: "Services",
             12: "Supplies", 13: "Auditing", 121: "Sole Source and Emergency",
             15: "Notices/Cooperative Purchasing"}   # 14 = For Sale (se omite)
SCBO_LABELS = ["Ad Title:", "Purchasing Agent/Entity:", "Ad Publish Date:",
               "Solicitation #:", "Direct Inquiries To:", "Bid/Submittal Due Date:",
               "Buyer Phone#:", "Buyer Email:", "Description:",
               "Pre-Bid Information:", "Full Details / Download:", "PRINT AD"]


def scbo(site, seen_ids, patterns, first=False):
    today = datetime.now(ET).strftime("%Y-%m-%d")
    items, done = [], set()
    for cid, cname in SCBO_CATS.items():
        url = f"https://scbo.sc.gov/online-edition?c={cid}-{today}"
        r = http_get(url)
        if r.status_code != 200:
            continue
        t = _html_to_text(r.text)
        for chunk in t.split("Ad Title:")[1:]:
            chunk = "Ad Title:" + chunk
            f = {}
            for i, lab in enumerate(SCBO_LABELS[:-1]):
                f[lab] = _field(chunk, lab, SCBO_LABELS[i + 1])
            title = f["Ad Title:"]
            key = short_hash("scbo", title, f["Purchasing Agent/Entity:"],
                             f["Solicitation #:"])
            if not title or key in done:
                continue
            done.add(key)
            more = f["Full Details / Download:"]
            items.append({
                "id": key,
                "ident": f["Solicitation #:"] if f["Solicitation #:"].upper() != "N/A" else "",
                "title": title,
                "title_extra": f["Description:"],
                "details": [("Entidad", f["Purchasing Agent/Entity:"]),
                            ("Categoría", cname),
                            ("Cierra", f["Bid/Submittal Due Date:"]),
                            ("Contacto", f["Buyer Email:"])],
                "link": more if more.startswith("http") else url,
                "body": f["Description:"],
            })
        time.sleep(0.5)
    return items


def fetch_site(site, seen_ids, patterns, first=False):
    s = site.lower()
    if "bonfirehub." in s:
        return bonfire_portal(site, seen_ids, patterns, first)
    if "phlcontracts.phila.gov" in s:
        return phl_contracts(site, seen_ids, patterns)
    if "/bso/" in s:
        return buyspeed(site, seen_ids, patterns, first)
    if "publish.gpo.gov/smallpurchase" in s:
        return gpo_small_purchase(site, seen_ids, patterns)
    if "columbusvendorservices.powerappsportals.com" in s:
        return columbus_vendor_services(site, seen_ids, patterns)
    if "clevelandohio.gov" in s:
        return cleveland(site, seen_ids, patterns, first)
    if "gwinnettcounty.com" in s:
        return gwinnett(site, seen_ids, patterns, first)
    if "doa.louisiana.gov/osp/lapac" in s:
        return louisiana(site, seen_ids, patterns, first)
    if "scbo.sc.gov" in s:
        return scbo(site, seen_ids, patterns, first)
    return generic_page(site, seen_ids, patterns)


# --------------------------------------------------------------------- correo
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


def source_label(site):
    u = urlparse(site)
    host = u.netloc.replace("www.", "")
    if "bonfirehub." in host:
        return "Bonfire · " + host.split(".")[0]
    return {
        "publish.gpo.gov": "GPO Small Purchase",
        "columbusvendorservices.powerappsportals.com": "City of Columbus, OH",
        "phlcontracts.phila.gov": "Philadelphia (PHLContracts)",
        "bidbuy.illinois.gov": "Illinois BidBuy",
        "gvibuy.buyspeed.com": "Islas Vírgenes (GVIBUY)",
        "njstart.gov": "New Jersey (NJSTART)",
        "clevelandohio.gov": "City of Cleveland, OH",
        "gwinnettcounty.com": "Gwinnett County, GA",
        "wwwcfprd.doa.louisiana.gov": "Louisiana (LaPAC)",
        "scbo.sc.gov": "South Carolina (SCBO)",
    }.get(host, host)


def build_email(hits, first_run):
    now = datetime.now(ET).strftime("%m/%d/%Y %I:%M %p ET")
    intro = ("Primera revisión: oportunidades abiertas que coinciden con tus "
             "palabras clave." if first_run else
             "Nuevas oportunidades que coinciden con tus palabras clave.")
    out = [f"<p style='font-family:Arial,sans-serif'>{escape(intro)}<br>"
           f"<small>{escape(now)}</small></p>"]
    current = None
    for h in hits:
        if h["source"] != current:
            current = h["source"]
            note = (" <small style='color:#888'>(primera revisión de este sitio: "
                    "incluye lo que ya estaba abierto)</small>"
                    if h.get("first_source") and not first_run else "")
            out.append(f"<h3 style='font-family:Arial,sans-serif;margin:22px 0 4px;"
                       f"border-bottom:2px solid #1a5276'>{escape(current)}{note}</h3>")
        head = (f"<b>{escape(h['ident'])}</b> — " if h["ident"] else "") \
            + f"<b>{escape(h['title'])}</b>"
        det = "<br>".join(f"{escape(k)}: {escape(str(v))}"
                          for k, v in h["details"] if v)
        kw = []
        if h["in_title"]:
            kw.append("<b>En el título / productos:</b> " + escape(", ".join(h["in_title"])))
        if h["in_body"]:
            kw.append("<b>En el documento:</b> " + escape(", ".join(h["in_body"])))
        out.append(
            "<div style='border:1px solid #ccc;border-radius:6px;padding:10px;"
            "margin:12px 0;font-family:Arial,sans-serif'>"
            f"{head}<br><small style='color:#444'>{det}</small><br>"
            f"<small>{'<br>'.join(kw)}</small><br>"
            f"<a href='{escape(h['link'])}'>Ver oportunidad</a></div>")
    return "\n".join(out)


def read_bonfire_hosts():
    if not BONFIRE_FILE.exists():
        return []
    hosts = []
    for line in read_list(BONFIRE_FILE):
        h = line.split("#")[0].strip().lower()
        h = urlparse(h if "://" in h else "https://" + h).netloc
        if h and h not in hosts:
            hosts.append(h)
    return [f"https://{h}/portal/?tab=openOpportunities" for h in hosts]


# ---------------------------------------------------------------------- main
def main():
    if not should_run():
        print("No toca revisar en esta hora (ajuste de horario de verano). Fin.")
        return 0

    sites = read_list(ROOT / "sites.txt")
    bonfire = read_bonfire_hosts()
    patterns = build_patterns(read_list(ROOT / "keywords.txt"))
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    first_run = not state
    print(f"Sitios: {len(sites)} · Portales Bonfire: {len(bonfire)}")

    hits, failures, bonfire_fail = [], [], []
    for site in sites + bonfire:
        is_bonfire = site in bonfire
        first = site not in state
        seen = set(state.get(site, []))
        t0 = time.time()
        try:
            items = fetch_site(site, seen, patterns, first)
            print(f"{site}: {len(items)} publicaciones ({time.time() - t0:.0f}s)")
            if not items and not is_bonfire and "gpo.gov" not in site:
                failures.append(site)
            for it in items:
                if it["id"] in seen or it.get("error"):
                    continue
                in_title = find_matches(
                    it["title"] + " ; " + it.get("title_extra", ""), patterns, flex=True)
                ignore = tuple(normalize(x) for x in it.get("body_ignore", ()))
                in_body = [k for k in find_matches(it["body"], patterns, flex=False)
                           if k not in in_title and not normalize(k).startswith(ignore or ("\0",))]
                if in_title or in_body:
                    hits.append({**it, "in_title": in_title, "in_body": in_body,
                                 "source": source_label(site), "first_source": first})
                seen.add(it["id"])
            # guardar solo lo que sigue publicado (evita que el archivo crezca)
            current = {it["id"] for it in items}
            state[site] = sorted(seen & current) if current else sorted(seen)
        except Exception as e:
            print(f"ERROR en {site}: {e}")
            (bonfire_fail if is_bonfire else failures).append(site)
        if is_bonfire:
            time.sleep(BONFIRE_PAUSE)

    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1))

    # Agrupar por sitio; dentro de cada sitio, primero las del título
    hits.sort(key=lambda h: (h["source"], not h["in_title"], h["title"]))
    print(f"Coincidencias nuevas: {len(hits)}")
    for h in hits:
        print(f" - [{h['source']}] {h['ident']} {h['title'][:70]} | título: "
              f"{h['in_title']} | documento: {h['in_body']}")
    if bonfire_fail:
        print(f"Portales Bonfire con error: {len(bonfire_fail)} de {len(bonfire)}")

    if os.getenv("DRY_RUN") == "1":
        return 0
    if hits:
        send_email(f"🔔 {len(hits)} oportunidad(es) con tus palabras clave",
                   build_email(hits, first_run))
    if failures or bonfire_fail:
        html = ""
        if failures:
            html += ("<p>No se pudo leer:</p><ul>"
                     + "".join(f"<li>{escape(f)}</li>" for f in failures) + "</ul>")
        if bonfire_fail:
            if len(bonfire_fail) <= 15:
                html += ("<p>Portales Bonfire con error:</p><ul>"
                         + "".join(f"<li>{escape(urlparse(f).netloc)}</li>"
                                   for f in bonfire_fail) + "</ul>")
            else:
                html += (f"<p>{len(bonfire_fail)} de {len(bonfire)} portales Bonfire "
                         "dieron error (posible bloqueo temporal de Bonfire).</p>")
        html += "<p>Puede que la página haya cambiado o esté caída.</p>"
        send_email("⚠️ El bot no pudo leer una página", html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
