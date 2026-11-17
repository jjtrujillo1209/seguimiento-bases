# Seguimiento de bases semanales

Cada semana se asignan ~50 empresas a cada sales rep para sus secuencias de Amplemarket.
Esta app cruza esa asignación con HubSpot y muestra, empresa por empresa, qué tratamiento
recibió desde el lunes de la semana asignada.

## Fuente principal: secuencias de Amplemarket

En la app, botón **Secuencias**: se marcan las secuencias de la semana y su número de semana ISO
(se guarda en `datos/secuencias.json`). Cada lead queda asignado a su **owner en Amplemarket**, así una
secuencia compartida (p. ej. Carlos y Nata) se reparte en una base por rep.

La API pública no lista los leads de una secuencia, así que se leen con la API interna de la web
desde un **Chrome dedicado** con tu sesión (patrón CDP del monorepo, puerto 9361):

```bash
bash chrome_amplemarket.sh   # la primera vez, iniciar sesión en Amplemarket ahí y dejarlo abierto
```

De Amplemarket se toma por lead: estado en la secuencia (en curso, por iniciar, terminó, rebotó…),
paso actual, primer contacto, aperturas, si respondió y el buzón que envía. Si Amplemarket ya contactó
y HubSpot no tiene la actividad, la empresa sale con la alerta **Sin registro en HubSpot**.

## Fuente alternativa: listas de leads

En Amplemarket, crear **una lista de leads por rep y por semana** con este nombre:

```
Base S45 · Andrea Jaramillo
```

- `S45` = semana ISO. El tratamiento se cuenta desde el lunes de esa semana.
- Después del separador (`·`, `-`, `|` o `:`) va el nombre o el email del rep en Amplemarket.
- También vale `Base Semana 45 2026 - baldovino@platzi.com`.
- Si una lista no sigue la convención, se registra en `datos/bases_manual.json`:
  `{"<id de la lista>": {"rep": "email@platzi.com", "semana": 45, "anio": 2026}}`

La API de Amplemarket no expone los leads de una secuencia; por eso la lista es la fuente de la base.

## Equipo y segmentos (`equipo.json`)

Lista de sellers con su segmento (`smb`, `mid_market`, `corporate`, `gobierno`, `sdr_ent`), equipo
(New Business, Account Management, SDR), región y líder. `segmentos.py` la aplica al leer los datos
(`/api/datos` y la publicación en Vercel), así que cambiar a alguien de segmento o de líder no exige
sincronizar. La UI filtra por segmento, líder y seller.

**Secuencias que se siguen solas.** Toda secuencia creada por un seller de `equipo.json` en las últimas
`SEMANAS_ACTIVAS` semanas entra al seguimiento con el creador como rep y la semana ISO de creación
(`seguimiento: auto`). Se excluyen los 5 borradores de ejemplo que Amplemarket crea al dar de alta a un
usuario (las plantillas «Branched Multi-Channel Sequence» y «Getting Started with AI Copy» y lo creado a
±10 min de ellas). También se siguen solas, con la misma ventana de semanas, las secuencias marcadas en Amplemarket con el tag
`seller_full_stack` (`TAG_SEGUIMIENTO`), las cree quien las cree: si el creador no es un seller quedan sin rep
fijo y sus leads se reparten por owner. Las secuencias creadas por otra persona para un seller se configuran en el panel
**Secuencias** con rep fijo. Las compartidas (sin rep fijo) se reparten por el owner de cada lead.

## Progreso por etapa de cada secuencia

En la pestaña **Secuencias**, al hacer clic en una secuencia se abre su progreso por etapa, como la tabla
«Progress» de Amplemarket con «Amplemarket user is Any»: tareas programadas, completadas, saltadas y
canceladas (y pausadas, si hay) de **todos los usuarios**, con un filtro por seller cuando hay más de uno.

- Fuente: API pública `GET /tasks` (`amplemarket.py · Amplemarket.tareas` y `progreso_por_etapa`). Obliga a
  pedir usuario por usuario y estado por estado, de a 20 tareas. Una vez al día se recorren todos los
  usuarios de cada secuencia vigente (varios minutos) y se anota quiénes tienen tareas (`tareas_usuarios`);
  las demás sincronizaciones del día consultan solo a esos, a los owners de los leads, al creador y al rep.
- La API no trae el número de etapa: la etapa es el tipo de tarea (y su ocurrencia por contacto si el tipo
  se repite), ordenada por creación de la tarea. Tampoco trae el día de la etapa.
- Los **correos automáticos no son tareas** y no salen en la tabla; mientras están programados se ven en
  «Leads en curso hoy» (el paso actual de cada lead, de la API interna).
- Pruebas: `.venv/bin/python -m unittest test_progreso`.

## Validación por contacto en HubSpot

Para cada contacto de una secuencia (`hs_trat` en `ultimo.json`):

- **Trabajado** en Amplemarket = tiene fecha de primer contacto.
- **Etapa en HubSpot**: No está en HubSpot → Contacto → Prospecto (`lifecyclestage=subscriber`) → Lead
  (etapa Lead/MQL/SQL u objeto Lead asociado) → Deal (deal abierto o nuevo asociado, u Opportunity/Customer).
- **Actividad en HubSpot**: correos, llamadas, mensajes y reuniones asociados al contacto desde el lunes de la base.
- **Brechas**: trabajado y no existe en HubSpot · trabajado sin actividad en HubSpot · respondió y sigue
  sin Lead ni Deal.

La pestaña **Contactos en HubSpot** lista contacto por contacto con su cadena y sus brechas; el CSV de esa
pestaña (y de Resumen) exporta los contactos.

## Qué cuenta como tratamiento (HubSpot)

Empresa ubicada por dominio y por la empresa asociada de sus contactos. Se leen las actividades
asociadas a la empresa **y** a los contactos de la base con fecha ≥ lunes de la semana:

| Estado | Regla |
|---|---|
| No está en HubSpot | Ni la empresa ni ningún contacto existen |
| Sin tocar | Existe, pero sin actividad desde el inicio |
| Contactada | Correo saliente, llamada, mensaje (LinkedIn/WhatsApp) o reunión |
| Respondió | Correo entrante, llamada conectada o `hs_sales_email_last_replied` del contacto |
| Reunión | Reunión registrada |
| Deal | Deal creado desde el inicio |

Alertas: owner en HubSpot distinto al rep, empresa sin owner, ya es cliente, deal abierto previo
(la guarda del monorepo), empresa duplicada, contactos fuera de HubSpot, actividades de otro owner.

## Uso

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env   # AMPLEMARKET_API_KEY y HUBSPOT_PRIVATE_APP_TOKEN
.venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port 8680
# En este Mac lo mantiene vivo launchd (com.platzi.seguimiento-bases); reiniciar con:
#   launchctl kickstart -k gui/$(id -u)/com.platzi.seguimiento-bases
# Estado de todos los servicios: ./07_OPERATIONS/servicios.sh status (desde la raíz)
```

Abrir http://127.0.0.1:8680 y pulsar **Sincronizar**. También por consola: `.venv/bin/python sincronizar.py`.

Cada sincronización guarda `datos/ultimo.json`, una copia en `datos/snapshots/` y agrega
el estado de cada empresa a `datos/historial.jsonl` (cambios de estado entre corridas).
Las bases con más de `SEMANAS_ACTIVAS` semanas quedan congeladas con su último dato.

## Actualización diaria automática

- La app sincroniza sola de lunes a viernes cada `SYNC_CADA_HORAS` (2) entre `SYNC_HORA` (7) y
  `SYNC_HORA_FIN` (19); los fines de semana, una vez desde las 7. Si el Mac estaba dormido, corre al
  despertar; si falla, reintenta cada `SYNC_REINTENTO_MIN` (60). Cada sync exitosa se publica en Vercel.
- Botón **Recargar**: vuelve a leer el último dato (en Vercel, la última foto publicada) sin sincronizar.
- Si el Chrome dedicado no está abierto, o quedó abierto sin ventanas (Cmd+W en Mac), la
  sincronización lo abre o le crea una pestaña. La sesión de Amplemarket vive en el perfil.
- Para que la app corra siempre, sin terminal abierta: `bash instalar_servicio.sh` (LaunchAgent
  `com.platzi.seguimiento-bases`; `--quitar` lo desinstala). Log en `~/Library/Logs/platzi/`.

## Versión en Vercel · https://follow-up-outbound.vercel.app

- Sitio estático (`publicar_vercel.py` arma `publico/`): `index.html` detecta que no está en
  localhost y lee `datos.json` / `historial.json`; sincronizar y configurar secuencias solo existen en local.
- Se publica solo después de cada sincronización exitosa (`PUBLICAR_VERCEL=1`). A mano:
  `.venv/bin/python publicar_vercel.py`.
- Protegido con contraseña en el Edge (`vercel/middleware.js`). La contraseña está en `.env`
  (`VERCEL_ACCESO_CLAVE`) y en Vercel como `ACCESO_CLAVE`; la sesión dura 30 días. Vercel
  Authentication no cubre la URL de producción en este plan, por eso el portón propio.
  El script verifica tras cada despliegue que `datos.json` no responda sin sesión.
- Proyecto `follow-up-outbound` en el equipo `juan-trujillo-s-projects1`.

## Higiene de correos y salud de envío (`entregabilidad.py`)

Amplemarket envía desde los buzones de Google Workspace de cada seller; esta app no envía ni reintenta.
Lo que controla es qué entra a una secuencia y cómo se mide lo que salió.

```bash
.venv/bin/python entregabilidad.py verificar base.csv   # escribe «base · verificada.csv» con decision y motivo
.venv/bin/python entregabilidad.py supresion            # reconstruye datos/supresion.json desde los snapshots
.venv/bin/python entregabilidad.py salud                # salud por secuencia del último dato
```

- **Decisión por correo:** `apto` (verificado por el proveedor y con MX), `revisar` (sin verificación vigente,
  catch-all, correo personal, buzón de rol, MX sin consultar) o `bloquear` (sintaxis, dominio inexistente o sin MX,
  inválido, duplicado, en supresión). Sintaxis + MX no prueban que el buzón exista.
- **Supresión global:** hard bounces, «sin email válido» y bajas vistos en cualquier snapshot; nadie sale de ella.
  El bloqueo por política no suprime la dirección. Las quejas de spam no llegan en el dato.
- **Umbrales** (`UMBRALES`, `MUESTRA_MINIMA`, `EVENTOS_MINIMOS`, `REVALIDAR_DIAS`): hipótesis operativas
  ajustables, no requisitos de los proveedores. «Pausar» es una recomendación: la pausa se hace en Amplemarket.

## Vistas ejecutiva y operativa (`gestiones.py`)

Botón **Gestiones** de la app (`/gestiones.html`; datos en `/api/gestiones`, y en Vercel `gestiones.json`, que se
publica con cada sincronización detrás del mismo portón): embudo por contacto y por cuenta, alertas, tendencia,
cortes (secuencia, responsable, segmento, cohorte, dominio remitente, proveedor receptor), la cola de gestiones y
el detalle de cada inscripción con sus enlaces a Amplemarket y HubSpot. Las definiciones de cada métrica están en
`DEFINICIONES` y en la propia vista. `.venv/bin/python gestiones.py vista` escribe la misma vista como HTML
autónomo en `~/Downloads/` (incrusta contactos: es un archivo local).

## Bases para sellers sin base (`armar_bases.py`)

`.venv/bin/python armar_bases.py [--simular]` arma 50 empresas (2 decisores cada una) para cada seller de
`equipo.json` que no tiene contactos en ninguna base seguida. Usa su base S46 preparada si existe (filtrada a su
segmento y región) y completa con lo ya prospectado: `prospeccion-2000` (base_2000 + decisores de Amplemarket) y
`SMB_disponibles_no_tratadas`. Segmento por empleados: SMB < 300 · Mid Market 300–999 · Corporate 1.000+;
región MX → solo México. Excluye dominios ya asignados (bases seguidas, todas las S46, repartos, cockpit,
lote-500) y, en HubSpot (solo lectura, dominio exacto), Customers, deals abiertos y actividad de ventas ≤ 90 días.
Escribe en `~/Downloads/Bases sellers sin base <fecha>/` la base, el CSV de importación a Amplemarket por seller,
`Resumen.csv` y `Excluidas por HubSpot.csv`.

**Etiqueta semanal.** Cada base se entrega como `S46.<Seller>.csv` (y `S46.<Seller> · Importar Amplemarket.csv`) con las
columnas `Tag` (`S46.<Seller>`), `Semana` y `Semana inicio` (lunes ISO) en cada fila; el CSV de importación lleva `tag`,
`semana` y `platzi_ref` para mapearlo como campo en Amplemarket. Las bases ya entregadas quedan excluidas en las corridas
siguientes.

## Filtro anti-rebote de las bases semanales (`filtro_rebote.py`)

Flujo: `armar_bases.py [--semanal]` → `enriquecer_bases.py [--semanal]` → importar **solo** `· Importar Amplemarket.csv`.
`armar_bases.py` ya no escribe el CSV de importación: sus correos son candidatos. Lo escribe `enriquecer_bases.py` con los
correos que pasan el filtro completo, y se niega a escribirlo si alguno no lo pasó.

Un correo pasa si: es «deliverable», no rebotó ni se dio de baja en ninguna secuencia, HubSpot no lo marca como rebote o
inválido, su dominio no tiene `spam_blocked` ni rebotes de otros contactos, tiene MX y la **revalidación** (≤ 30 días,
1 crédito por correo) dice «deliverable» y **no catch-all**. Si un dato falta, no pasa. La revalidación la hace
ZeroBounce si `ZEROBOUNCE_API_KEY` está en `.env` (solo «valid» pasa; lote en `bulkapi` y, si responde 403, correo por
correo; revisa el saldo antes de cobrar) y, si no, Amplemarket (`/email-validations`).

```bash
.venv/bin/python filtro_rebote.py                      # rebote por base y por cohorte de filtro (¿llegó a 0?)
.venv/bin/python filtro_rebote.py verificar base.csv   # cualquier CSV antes de importarlo → «· apto.csv» y «· descartados.csv»
```

Ninguna validación garantiza 0 %: la meta es que la cohorte «filtro completo» del diagnóstico quede en 0 y, si algo rebota
ahí, el diagnóstico lista el dominio para revisarlo. Los rebotes nuevos entran solos a la supresión en la siguiente
sincronización. Pruebas: `.venv/bin/python -m unittest test_filtro_rebote`.
