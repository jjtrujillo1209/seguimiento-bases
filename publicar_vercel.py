"""Publica la foto del seguimiento en Vercel (proyecto «follow-up-outbound»).

La sincronización necesita el Chrome dedicado, así que solo corre en este Mac; Vercel recibe
un sitio estático con datos.json e historial.json. La app lo llama después de cada
sincronización (PUBLICAR_VERCEL=1 en .env). A mano:

    .venv/bin/python publicar_vercel.py            # publica el último dato
    .venv/bin/python publicar_vercel.py --vacio    # publica el sitio sin datos

Los datos traen contactos y actividad de HubSpot: el sitio vive detrás de un portón con
contraseña en el Edge (vercel/middleware.js; ACCESO_CLAVE y SESSION_SECRET en Vercel). Vercel
Authentication no cubre la URL de producción en este plan. Después de cada despliegue se verifica que datos.json NO responda sin sesión;
si respondiera, se vuelve a publicar vacío y se falla.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

import gestiones
import segmentos

RAIZ = Path(__file__).parent
load_dotenv(RAIZ / ".env")
PUBLICO = RAIZ / "publico"
PROYECTO = os.getenv("VERCEL_PROYECTO", "follow-up-outbound")
SCOPE = os.getenv("VERCEL_SCOPE", "")
URL = f"https://{PROYECTO}.vercel.app"
PATH_NODE = f"{Path.home()}/.local/node/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

VERCEL_JSON = {
    "$schema": "https://openapi.vercel.sh/vercel.json",
    "headers": [{"source": "/(.*)", "headers": [
        {"key": "X-Robots-Tag", "value": "noindex, nofollow"},
        {"key": "Cache-Control", "value": "private, no-store"},
        {"key": "X-Content-Type-Options", "value": "nosniff"},
        {"key": "Referrer-Policy", "value": "no-referrer"},
    ]}],
}


def programacion() -> str:
    """Texto de la frecuencia de actualización (la misma regla que el programador de api.py)."""
    ini, fin = int(os.getenv("SYNC_HORA", "7")), int(os.getenv("SYNC_HORA_FIN", "19"))
    cada = float(os.getenv("SYNC_CADA_HORAS", "2"))
    return f"se actualiza sola cada {cada:g} h de {ini}:00 a {fin}:00 (lun–vie)"


def _historial_por_empresa() -> dict[str, list[dict]]:
    """{"lista_id|clave": [cambios de estado]}, lo mismo que /api/historial pero precalculado."""
    res, previo = {}, {}
    ruta = RAIZ / "datos" / "historial.jsonl"
    if ruta.exists():
        for linea in ruta.read_text().splitlines():
            r = json.loads(linea)
            k = f"{r['lista_id']}|{r['clave']}"
            if r["estado"] != previo.get(k):
                res.setdefault(k, []).append({"ts": r["ts"], "estado": r["estado"]})
                previo[k] = r["estado"]
    return res


def armar(vacio: bool = False) -> None:
    vinculo = PUBLICO / ".vercel"
    guardado = None
    if vinculo.exists():
        guardado = RAIZ / "datos" / ".vercel-vinculo"
        shutil.rmtree(guardado, ignore_errors=True)
        shutil.copytree(vinculo, guardado)
    shutil.rmtree(PUBLICO, ignore_errors=True)
    (PUBLICO / "static").mkdir(parents=True)
    if guardado:
        shutil.copytree(guardado, vinculo)
        shutil.rmtree(guardado)
    shutil.copy(RAIZ / "static" / "index.html", PUBLICO / "index.html")
    shutil.copy(RAIZ / "vercel" / "middleware.js", PUBLICO / "middleware.js")
    shutil.copy(RAIZ / "static" / "platzi-tokens.css", PUBLICO / "static" / "platzi-tokens.css")
    datos = {"generado": None, "bases": [], "avisos": []}
    if not vacio and (RAIZ / "datos" / "ultimo.json").exists():
        datos = json.loads((RAIZ / "datos" / "ultimo.json").read_text())
    segmentos.aplicar(datos)
    datos["programacion"] = programacion()
    (PUBLICO / "datos.json").write_text(json.dumps(datos, ensure_ascii=False))
    # Vistas ejecutiva y operativa: misma plantilla y el mismo cálculo que /api/gestiones
    shutil.copy(RAIZ / "static" / "gestiones.html", PUBLICO / "gestiones.html")
    hay_datos = not vacio and (RAIZ / "datos" / "ultimo.json").exists()
    vistas = gestiones.cargar() if hay_datos else {"generado": None, "filas": []}
    (PUBLICO / "gestiones.json").write_text(json.dumps(vistas, ensure_ascii=False))
    (PUBLICO / "historial.json").write_text(json.dumps({} if vacio else _historial_por_empresa(), ensure_ascii=False))
    (PUBLICO / "vercel.json").write_text(json.dumps(VERCEL_JSON, indent=2))


def _vercel(*args: str) -> str:
    extra = ["--scope", SCOPE] if SCOPE else []
    r = subprocess.run(["vercel", *args, *extra], cwd=PUBLICO, capture_output=True, text=True,
                       env=os.environ | {"PATH": PATH_NODE}, timeout=600)
    if r.returncode:
        raise RuntimeError(f"vercel {' '.join(args)} falló: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout.strip()


def _publico_sin_sesion() -> bool:
    try:
        urllib.request.urlopen(urllib.request.Request(f"{URL}/datos.json", method="HEAD"), timeout=30)
        return True
    except urllib.error.HTTPError as e:
        return e.code < 300
    except urllib.error.URLError:
        return False


def publicar(vacio: bool = False) -> str:
    armar(vacio)
    if not (PUBLICO / ".vercel" / "project.json").exists():
        _vercel("link", "--yes", "--project", PROYECTO)
    _vercel("deploy", "--prod", "--yes")
    if not vacio and _publico_sin_sesion():
        armar(vacio=True)
        _vercel("deploy", "--prod", "--yes")
        raise RuntimeError(f"{URL} quedó público sin login; lo volví a publicar vacío. "
                           "Revisa ACCESO_CLAVE y SESSION_SECRET en el proyecto antes de publicar datos.")
    return URL


if __name__ == "__main__":
    print(publicar(vacio="--vacio" in sys.argv))
