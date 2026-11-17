# Cambios · seguimiento-bases

## 2026-11-17 · Filtro anti-rebote obligatorio en las bases semanales

Causa: «deliverable» del enriquecimiento rebotó 3,9 % (13 de 330), en servidores propios (9,3 %) y Microsoft 365
(3,9 %) y nunca en Google (0 de 57): patrón de dominios catch-all. La revalidación que los detecta era opcional y no corrió
en S47 (381 correos sin ella). Además `revalidar()` leía solo la primera página de resultados y `armar_bases.py` escribía
un CSV de importación sin filtrar.

- `filtro_rebote.py`: revalidación obligatoria (sin ella nada pasa), resultados con fecha y vencimiento a 30 días, lectura
  de todas las páginas, lotes interrumpidos que se retoman sin volver a pagar, MX que falla cerrado (con caché), bajas en la
  supresión, 1 rebote en el dominio basta para excluirlo (antes 2: el otro contacto rebota 18 % contra 4,7 %),
  `verificar <csv>` para cualquier archivo y diagnóstico por cohorte de filtro.
- `enriquecer_bases.py`: sin `--sin-revalidar`; candado que impide escribir el CSV de importación si un correo no pasó.
- `armar_bases.py`: ya no escribe `· Importar Amplemarket.csv`.
- `test_filtro_rebote.py`: 14 pruebas.
- `.gitignore`: `datos/enriquecimiento/`, `datos/validaciones/` y `datos/exclusion_amplemarket.json` (PII).

Aplicado a S47 el 17-nov-2026: los 381 correos pasan las capas gratuitas; la revalidación de Amplemarket respondió
402 (sin créditos de validación, no se cobró nada).

Después, mismo día: ZeroBounce como validador (`ZEROBOUNCE_API_KEY`; `bulkapi` dio 403 a esta cuenta, así que valida
correo por correo). Con los 100 créditos disponibles se validaron 103 correos de S47 (los «unknown» no cobran):
94 valid, 5 catch-all, 1 invalid, 3 unknown. Resultado: Andrea Jaramillo 51 aptas de 58, Camilo Baldovino 22 de 25,
Patricia Salgar 20 de 22 (2 sin revalidar). Las otras 4 bases de S47 (276 correos) siguen sin filtro completo.

## 2026-11-16 · Higiene de correos, salud de envío y vistas de gestiones

Conectado a la app y a Vercel: `api.py` expone `/api/gestiones` y `/gestiones.html`, `publicar_vercel.py`
publica `gestiones.html` y `gestiones.json` (detrás del mismo portón) y `index.html` enlaza la vista con el
botón **Gestiones**.

- `entregabilidad.py`: revisión previa de una base (sintaxis, dominio, MX, supresión, duplicados, verificación
  vencida), supresión global en `datos/supresion.json`, estado técnico del envío separado del comercial,
  clasificación de fallos (permanente, política, temporal) y salud de envío con umbrales de aviso y pausa.
- `gestiones.py` + `static/gestiones.html`: vistas ejecutiva y operativa armadas desde `datos/ultimo.json`.
  Lo que el dato no trae sale como «sin datos» con la forma de instrumentarlo.
- `test_entregabilidad.py`: 17 pruebas. `.venv/bin/python -m unittest test_entregabilidad test_progreso`.
- `.gitignore`: `datos/supresion.json` y `datos/cache_mx.json` (PII de runtime).

Medido el 16-nov-2026 sobre 1.449 inscripciones (primer contacto del 4 al 16 de noviembre):
1.082 contactados, 50 rebotes duros (4,6 %), 18 bloqueos por política (1,7 %), 1.082 de 1.082 contactados
sin verificación del correo. Los 50 rebotes fueron en dominios con MX: la revisión de MX no los habría
evitado; la verificación de buzón sí es la palanca.

Pendiente de aprobación: guardar el id del lead de
Amplemarket (hoy `agrupar_empresas` lo descarta) y llamar a `revisar_base` desde `armar_bases.py`.
