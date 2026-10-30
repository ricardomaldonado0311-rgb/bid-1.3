# Bid Searcher — bot de oportunidades por palabras clave (v1.3)

Revisa sitios de compras de gobierno de lunes a viernes a las **12:00 a. m. y 4:00 p. m. (hora del Este)** y manda un correo **solo** con las oportunidades nuevas que contengan alguna palabra de `keywords.txt`. Si una página deja de leerse, también avisa.

Corre gratis en GitHub Actions. No necesitas la computadora encendida.

## Archivos

| Archivo | Para qué sirve |
|---|---|
| `monitor.py` | El bot (un lector para cada sitio). |
| `sites.txt` | Sitios que vigila (una URL por línea). |
| `bonfire_sites.txt` | Portales Bonfire (uno por línea; `#` delante = desactivado). |
| `keywords.txt` | Palabras clave (una por línea). |
| `requirements.txt` | Librerías de Python. |
| `.github/workflows/monitor.yml` | Horario y pasos de la corrida en GitHub. |
| `state/seen.json` | Lo que el bot ya vio. Lo actualiza solo; **no lo borres**. |
| `CONTEXTO.md` | Notas técnicas de cada sitio (para seguir trabajando con Claude). |

## Instalación en un repositorio nuevo (~15 min)

### 1. Contraseña de aplicación de Gmail
1. En la cuenta de Gmail que enviará los avisos, activa la **verificación en 2 pasos**.
2. Entra a <https://myaccount.google.com/apppasswords>, crea una llamada `bot` y copia la clave de 16 letras.

### 2. Crear el repositorio y subir los archivos
1. En GitHub: **New repository** → nombre (por ejemplo `BID-1.3`) → **Private** → **Create repository**.
2. **Add file → Upload files**, arrastra **todo el contenido** de esta carpeta (incluidas las carpetas `.github` y `state`) y pulsa **Commit changes**.
3. Revisa que exista `.github/workflows/monitor.yml`. Si la carpeta `.github` no se subió (en Mac/Windows está oculta):
   **Add file → Create new file**, escribe el nombre `.github/workflows/monitor.yml`, pega el contenido del archivo y guarda.

### 3. Secretos (datos del correo)
**Settings → Secrets and variables → Actions → New repository secret**:

| Nombre | Valor |
|---|---|
| `SMTP_USER` | el Gmail que envía (ej. `tucuenta@gmail.com`) |
| `SMTP_PASSWORD` | la clave de 16 letras del paso 1 |
| `EMAIL_TO` | dónde recibir los avisos (varios separados por coma) |

### 4. Permiso para guardar lo ya visto
**Settings → Actions → General → Workflow permissions** → marca **Read and write permissions** → **Save**.
(Sin esto, el último paso de la corrida falla y el bot repetiría los mismos avisos.)

### 5. Primera prueba
**Actions → Monitor de oportunidades → Run workflow**.
- Tarda unos **15 minutos** (revisa ~300 portales con pausas).
- El primer correo es **grande**: trae todo lo que ya está abierto y coincide. Desde la siguiente corrida, solo lo nuevo.
- En la primera vuelta de cada sitio nuevo solo se revisan títulos; desde la segunda también se leen los PDF y detalles.

## Uso diario
- **Añadir palabras clave:** en GitHub abre `keywords.txt`, lápiz ✏️, una palabra por línea, **Commit changes**.
- **Añadir un sitio:** en `sites.txt` (o en `bonfire_sites.txt` si es Bonfire), una URL por línea. Un sitio sin lector propio se lee de forma genérica.
- **Desactivar un portal Bonfire:** pon `#` al inicio de su línea.
- **Ver qué pasó:** pestaña **Actions** → la corrida → paso **Revisar páginas** (cuántas publicaciones leyó de cada sitio y las coincidencias).
