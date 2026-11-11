# Contexto técnico — Bid Searcher GPO (v2.0, 06-nov-2026)

Bot en Python que corre en GitHub Actions (repositorio privado `bid-1.3`). Desde la v2.0 vigila **solo GPO Small Purchase**. Las versiones 1.x (Columbus, Philadelphia, Bonfire, etc.) quedaron en el historial del repositorio.

## Fuente de datos (GPO)
- Lista: GET `https://publish.gpo.gov/api/ContractorConnection/GetOpenJobs` → `value` (JSON público). Campos usados: `jacketNumber`, `fRevised` (R-1, R-2…), `jobId`, `title`, `productType`, `totalQuantity`, `quoteDueDate`, `datePosted`, `shipDeliveryDate`.
- RFQ: GET `/api/Files/GetRfqContainerSettings?containerName=opportunities-and-pricing&blobName={jobId}/{jobId}_{jacket}.pdf` → `value.blobSasUrl` → PDF (pypdf, primeras 40 páginas).
- Enlace para el usuario: `https://publish.gpo.gov/attachments/rfq/{jobId}/{jacket}`.
- Si un jacket aparece varias veces, se toma la revisión más alta.

## Lógica
- Clave de cada bid: número de jacket.
- **Nuevo** = jacket nunca visto. Se avisa siempre; ★ si coincide con `keywords.txt` (título con singular/plural; documento con frase exacta, sin contar "jacket").
- **Revisión de un bid ajeno** (sube `fRevised`): se avisa en la sección de nuevos.
- **Mis bids** (`mis_bids.txt`): en cada corrida se comparan todos los campos simples del JSON (menos `jobId`) y el texto del PDF (hash + diff por líneas). Se avisa de cualquier cambio, de la desaparición de la lista de abiertos y de la reaparición.
- Al añadir un bid a `mis_bids.txt` se guarda su estado actual como base (sin aviso de cambio) y llega un correo "📌 Ahora sigo".
- Al editar `mis_bids.txt`, el workflow corre solo (trigger `push` con `paths`).
- `state/gpo.json`: `jobs` (campos completos de cada bid, hash del PDF; las líneas del PDF solo de mis bids) y `tracked` (desde cuándo y último cambio). Los bids cerrados que no son míos se borran a los 90 días.
- `mis_bids_estado.md`: tabla generada en cada corrida.

## Pendiente / ideas
- Si GPO devuelve campos que cambian solos y causan falsos avisos, añadirlos a `IGNORE_FIELDS`.
- Posible: avisar también de adjudicaciones (awards) si GPO las publica.
- Horario (06-nov-2026): cada hora de 7 a. m. a 7 p. m. ET, lunes a viernes (cron `17 11-23 * * 1-5`).
  Motivo: los 23 bids abiertos el 06-nov se publicaron entre 7:25 a. m. y 4:35 p. m. ET, repartidos a lo
  largo del día, y casi todos cierran 2 o 3 días hábiles después (muchos el lunes a las 10 a. m.).
  Antes (12 a. m. y 4 p. m.), un bid publicado el viernes por la tarde se avisaba hasta el lunes a las 12 a. m.
  Nota: `should_run()` antes leía la hora del texto del cron y siempre devolvía True (corría 4 veces al día).
  Para reevaluar: `first_seen` y `datePosted` de cada bid en `state/gpo.json`.
- Fines de semana (06-nov-2026): JC pidió revisar también sábado y domingo durante un mes. El 07-dic-2026
  se revisa `datePosted`: si ningún bid se publicó en fin de semana, se vuelve a lunes a viernes
  (cron `17 11-23 * * 1-5` y `RUN_DAYS_ET = {0, 1, 2, 3, 4}`).

## Uni and Others (11-nov-2026)
- `uni.py` + `.github/workflows/uni.yml`. Reusa helpers de `monitor.py` (correo, palabras clave, http).
- API: `https://<host>/PublicPortal/getOpenPublicOpportunitiesSectionData` → `payload.projects`
  (ProjectID, ReferenceID, ProjectName, DepartmentID, DateClose en UTC) y `payload.departments`.
- Estado `state/uni.json`: `portals[host] = {ids: {ProjectID: fecha_vista}, fails, checked, reported}`.
- Cron: 4 líneas (15, 16, 21, 22 UTC); `should_run()` lee la hora de `CRON_SCHEDULE` y elige según horario de verano.
- 429/403/503 de Bonfire: se reintenta al final tras 60 s. Si falla más de la mitad, no se guarda estado y llega aviso.
- Lista de JC (11-nov): 296 portales Bonfire únicos. Quitados: vendor.bonfirehub.com (buscador, no portal),
  dfwairport-sandbox (portal de pruebas). Pendientes de link correcto: Beaufort-Jasper Water & Sewer Authority,
  Forty Niners Stadium Management Company, Superior Court of California County of Monterey.
  No Bonfire (pendientes): norta.procureware.com/Bids (ProcureWare), supplier.ionwave.net (IonWave, link genérico).
- Ambos workflows hacen commit al mismo repo: el push reintenta con `git pull --rebase` hasta 5 veces.
