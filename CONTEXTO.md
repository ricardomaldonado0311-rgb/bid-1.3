# Contexto del proyecto: bot de oportunidades por palabras clave

Pega este texto en la nueva sesión de Claude junto con el archivo bot-oportunidades.zip.

## Qué es
Bot en Python que corre gratis en GitHub Actions (repositorio privado `BID-1.1`). Revisa sitios de compras de gobierno de lunes a viernes a las 12:00 a. m. y 4:00 p. m. hora del Este, y manda un correo (Gmail SMTP) solo con las oportunidades NUEVAS que contengan palabras de `keywords.txt`. Lo ya visto se guarda en `state/seen.json`.

## Archivos
- `monitor.py`: lógica principal. Tiene un lector específico por sitio (`fetch_site`) y uno genérico con Playwright para cualquier otra URL.
- `keywords.txt`: 164 palabras clave (artículos promocionales, ropa, PPE, oficina, etc.). Una por línea; `#` = comentario.
- `sites.txt`: una URL por línea.
- `.github/workflows/monitor.yml`: horario (cron UTC doble para horario de verano/invierno; `should_run()` decide cuál vale).
- Secretos en GitHub: `SMTP_USER`, `SMTP_PASSWORD` (contraseña de aplicación de Gmail), `EMAIL_TO`.

## Coincidencias
- Título / productos: insensible a mayúsculas, guiones y singular/plural.
- Cuerpo del documento: frase exacta (para evitar falsos avisos).
- El correo separa "en el título / productos" y "en el documento".

## Sitios ya probados e integrados
1. **GPO Small Purchase** – https://publish.gpo.gov/smallPurchase?activeTab=Opportunities
   - Lista: GET `https://publish.gpo.gov/api/ContractorConnection/GetOpenJobs` (JSON público).
   - RFQ PDF: GET `/api/Files/GetRfqContainerSettings?containerName=opportunities-and-pricing&blobName={jobId}/{jobId}_{jacket}.pdf` → `value.blobSasUrl` → PDF (pypdf).
   - ID = jacket + revisión (R-1, R-2 vuelven a avisar). ✅ Probado en GitHub, correo recibido.
2. **City of Columbus** – https://columbusvendorservices.powerappsportals.com/OpenRFQs/
   - API pública Power Pages: GET `/_api/cr820_rfqheaders` con `$select`, `$expand=cr820_RFQLine_RFQHeader_cr820_RFQHeader(...)` y `$filter=(cr820_expirydateandtime ge <ahora UTC>)`.
   - Busca en título + productos de las líneas + descripción. Probado en navegador (85 abiertas, 5 coincidencias).
3. **Philadelphia PHLContracts** – https://www.phlcontracts.phila.gov/bso/view/search/external/advancedSearchBid.xhtml
   - Lista: búsqueda JSF con Playwright, estado "Sent" (valor `2BS` en `bidSearchForm:status`), paginación `a.ui-paginator-next` (25 por página, ~185 abiertas).
   - Detalle público: GET `/bso/external/bidDetail.sda?docId={bid}&external=true&parentUrl=close`; se lee "Description:", "Department:", "Bid Opening Date:" y los artículos desde "Item # 1" (quitando el texto "NIGP Code: …").
   - Probado paso a paso en Chrome; FALTA confirmar la corrida completa en GitHub.

## Pendientes
- Subir la última versión de `monitor.py` y `sites.txt` a GitHub, correr "Run workflow" y confirmar que llega el correo con Columbus + Philadelphia.
- Seguir probando nuevas páginas una por una (abrirlas en Chrome, encontrar la fuente de datos, probar palabras clave y añadir un lector en `monitor.py`).
- Sugerencia: añadir `bags` o `gym bag` a keywords (una oportunidad "Gym Bag" de GPO no saltó).
- Sitios con log in: posible si es usuario/contraseña simple (secretos de GitHub); no con CAPTCHA; con 2FA solo reutilizando sesión.


## Versión 1.3 (30-oct-2026)
- `monitor.yml`: servidor fijo en `ubuntu-24.04` (Ubuntu 26 rompía la instalación de Playwright) y `timeout-minutes: 30`.
- Lectores nuevos en `monitor.py`:
  - **Bonfire** (`bonfire_sites.txt`, ~295 portales de EE. UU.; Canadá desactivado con `#`): GET `https://<host>/PublicPortal/getOpenPublicOpportunitiesSectionData` (JSON público). Pausa de 1.5 s entre portales. Si un portal responde HTML en vez de JSON = sin portal público (se ignora). Los errores de Bonfire se resumen en un solo aviso.
  - **BuySpeed** (Illinois BidBuy, NJSTART, Islas Vírgenes GVIBUY): `advancedSearchBid.xhtml?openBids=true` con Playwright, solo estado "Sent"; detalle público `bidDetail.sda` como Philadelphia.
  - **Cleveland**: invitations-bid y request-qualificationsproposal (`div.accordion-item`, PDF del bid).
  - **Gwinnett County**: `li.list-group-item` + PDF.
  - **Louisiana LaPAC**: `catbids.cfm` → cada categoría `dspBid.cfm?search=segment&term=<UNSPSC>` (tabla `table.bid`) + PDF.
  - **South Carolina SCBO**: `online-edition?c=<categoría>-<AAAA-MM-DD>` (fecha de hoy, todas las categorías menos "For Sale").
- Primera revisión de cada sitio nuevo: no descarga PDFs/detalles (solo título) para no tardar; desde la segunda, sí.
- GPO: "jacket" en el documento se ignora (es el número de trabajo).
- Palabras nuevas: `bags`, `gym bags`. Si coincide "tote bags" ya no repite "bags".
- El correo agrupa por sitio.
- Probado en navegador: sitios públicos sin login. Revisados y descartados: OhioBuys, eMMA, eVA (verificación anti-bot), OpenGov Norfolk/DC (Cloudflare), Rhode Island (robots.txt), Baltimore (login).
