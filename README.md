# Bid Searcher — GPO Small Purchase

Vigila **solo GPO Small Purchase** (publish.gpo.gov) de lunes a viernes a las
**todos los días, cada hora de 7 a. m. a 7 p. m. (hora del Este)** y manda un correo con:

1. **🔄 Cambios en tus bids**: los de `mis_bids.txt`. Avisa de una nueva revisión
   (R-1, R-2…), un cambio de fecha de cotización, cantidad o entrega, un cambio en
   el documento RFQ (con las líneas nuevas y quitadas) o si el bid ya no aparece
   en la lista de abiertos.
2. **🆕 Bids nuevos**: **todos** los que publique GPO, sin importar las palabras clave.
   Los que coinciden con `keywords.txt` salen marcados con ★ y van primero.
3. **📌 Ahora sigo**: confirmación cuando añades un bid a tu lista.

Si no hay nada nuevo, no manda correo.

## Tu mini base de datos

| Archivo | Para qué |
|---|---|
| `mis_bids.txt` | **Tú lo editas.** Los jackets que trabajaste, uno por línea, con nota opcional después de `|`. |
| `mis_bids_estado.md` | **Lo escribe el bot.** Tabla con el estado de tus bids: revisión, fecha de cotización, si sigue abierto y el último cambio. |
| `state/gpo.json` | Memoria del bot (todo lo que ha visto en GPO). No lo borres. |

### Añadir un bid
1. En GitHub abre `mis_bids.txt` → lápiz ✏️.
2. Añade una línea, por ejemplo: `500414 | DBT Worksheets Book - cotizado 11/02`.
3. **Commit changes**. El bot corre solo en ese momento y te manda un correo
   "📌 Ahora sigo…" para confirmar.

Para dejar de seguir un bid, borra su línea.

## Otros archivos
- `keywords.txt`: palabras clave para marcar con ★. Pon `=` delante para buscar la palabra exacta (`=caps`).
- `monitor.py`: el bot. `requirements.txt`: librerías.
- `.github/workflows/monitor.yml`: horario y pasos.

## Instalación (si es un repositorio nuevo)
1. **Settings → Secrets and variables → Actions**: `SMTP_USER`, `SMTP_PASSWORD` (contraseña de aplicación de Gmail), `EMAIL_TO`.
2. **Settings → Actions → General → Workflow permissions → Read and write permissions**.
3. **Actions → Bid Searcher GPO → Run workflow**.

## Bot "Uni and Others"
Segundo bot, en este mismo repositorio. Revisa los portales Bonfire de
`uni_portales.txt` (universidades, distritos escolares y otras agencias)
**a las 11:00 a. m. y 5:00 p. m. (hora del Este), de lunes a viernes**.

- Avisa por correo de **todas** las oportunidades nuevas; las que coinciden con
  `keywords.txt` llevan ★ y van primero. Si no hay nada nuevo, no escribe.
- La primera vez solo manda las abiertas que coinciden con las palabras clave.
- Si un portal falla 3 revisiones seguidas, avisa una vez para que se corrija el link.
- Para añadir o quitar portales, edita `uni_portales.txt` (uno por línea).
- Para probarlo a mano: **Actions → Uni and Others → Run workflow**.
- Archivos: `uni.py` (el bot), `uni_portales.txt`, `state/uni.json` (memoria),
  `.github/workflows/uni.yml` (horario).
