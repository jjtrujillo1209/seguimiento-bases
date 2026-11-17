"""Seguimiento de bases semanales · Amplemarket → HubSpot.

Arranque:  .venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port 8680
"""
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import gestiones
import publicar_vercel
import segmentos
import sincronizar as motor
from amplemarket import Amplemarket, guardar_secuencias

RAIZ = Path(__file__).parent
app = FastAPI(title="Seguimiento de bases")

# Sincronización automática. Lunes a viernes: cada SYNC_CADA_HORAS entre SYNC_HORA y SYNC_HORA_FIN.
# Fin de semana: una vez al día desde SYNC_HORA. Si el Mac estaba dormido, corre al despertar;
# si falla, reintenta cada SYNC_REINTENTO_MIN. Cada sincronización exitosa se publica en Vercel.
SYNC_HORA = int(os.getenv("SYNC_HORA", "7"))
SYNC_HORA_FIN = int(os.getenv("SYNC_HORA_FIN", "19"))
SYNC_CADA_HORAS = float(os.getenv("SYNC_CADA_HORAS", "2"))
SYNC_REINTENTO_MIN = int(os.getenv("SYNC_REINTENTO_MIN", "60"))

estado_sync = {"corriendo": False, "mensajes": [], "error": None, "inicio": None, "fin": None,
               "origen": None, "programacion": publicar_vercel.programacion()}
candado = threading.Lock()


def _correr():
    def avisar(m):
        estado_sync["mensajes"] = (estado_sync["mensajes"] + [m])[-30:]
    try:
        motor.sincronizar(avisar)
        if os.getenv("PUBLICAR_VERCEL") == "1":
            avisar("Publicando en Vercel…")
            avisar(f"Publicado en {publicar_vercel.publicar()}")
    except Exception as exc:  # se muestra en la UI tal cual
        estado_sync["error"] = str(exc)
    finally:
        estado_sync["corriendo"] = False
        estado_sync["fin"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lanzar(origen: str) -> dict:
    with candado:
        if estado_sync["corriendo"]:
            return estado_sync
        estado_sync.update(corriendo=True, mensajes=[], error=None, fin=None, origen=origen,
                           inicio=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        threading.Thread(target=_correr, daemon=True).start()
    return estado_sync


def _ultima_sync() -> datetime | None:
    try:
        generado = json.loads((RAIZ / "datos" / "ultimo.json").read_text()).get("generado")
        return datetime.fromisoformat(generado).astimezone() if generado else None
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None


def _toca_sincronizar(ahora: datetime) -> bool:
    if ahora.hour < SYNC_HORA:
        return False
    ultima = _ultima_sync()
    if not ultima or ultima.date() != ahora.date():
        return True  # la primera del día, también fines de semana y aunque ya sea tarde
    laborable = ahora.weekday() < 5 and ahora.hour < SYNC_HORA_FIN
    return laborable and ahora - ultima >= timedelta(hours=SYNC_CADA_HORAS)


def _programador():
    while True:
        ahora = datetime.now().astimezone()
        reciente = estado_sync["inicio"] and datetime.fromisoformat(estado_sync["inicio"]) > \
            datetime.now(timezone.utc) - timedelta(minutes=SYNC_REINTENTO_MIN)
        if _toca_sincronizar(ahora) and not estado_sync["corriendo"] and not reciente:
            _lanzar("automática")
        time.sleep(300)


@app.on_event("startup")
def _arrancar_programador():
    threading.Thread(target=_programador, daemon=True).start()


@app.post("/api/sincronizar")
def iniciar_sync():
    return _lanzar("manual")


@app.get("/api/sincronizar")
def ver_sync():
    return estado_sync


@app.get("/api/datos")
def datos():
    ruta = RAIZ / "datos" / "ultimo.json"
    if not ruta.exists():
        return segmentos.aplicar({"generado": None, "bases": [], "avisos": []})
    return segmentos.aplicar(json.loads(ruta.read_text()))


@app.get("/api/equipo")
def ver_equipo():
    """Sellers con su segmento, equipo, región y líder (equipo.json)."""
    return {"segmentos": segmentos.SEGMENTOS, "sellers": segmentos.cargar_equipo()}


@app.get("/api/secuencias")
def secuencias():
    """Todas las secuencias de Amplemarket, con la semana asignada si se están siguiendo."""
    config = motor.secuencias_seguidas()
    am = Amplemarket(os.getenv("AMPLEMARKET_API_KEY", "").strip())
    filas = am.secuencias()
    reps = sorted(({"email": u["email"], "nombre": u.get("name") or u["email"]}
                   for u in am.usuarios() if u.get("status") == "active"), key=lambda u: u["nombre"])
    return {"reps": reps, "secuencias": [{
        "id": s["id"], "nombre": s["name"], "estado": s["status"], "creador": s.get("created_by_user_email"),
        "creada": s.get("created_at"), "url": s.get("url"),
        "seguida": s["id"] in config, **config.get(s["id"], {}),
    } for s in sorted(filas, key=lambda s: s.get("created_at") or "", reverse=True)]}


@app.post("/api/secuencias")
def guardar(config: dict[str, dict]):
    """Reemplaza la configuración: {secuencia_id: {"semana": 45, "anio": 2026}}."""
    limpio = {}
    for sid, c in config.items():
        try:
            semana, anio = int(c["semana"]), int(c["anio"])
            motor.lunes_iso(anio, semana)
        except (KeyError, ValueError, TypeError):
            raise HTTPException(400, f"Semana inválida para la secuencia {sid}")
        limpio[sid] = {"semana": semana, "anio": anio}
        if c.get("rep"):
            limpio[sid]["rep"] = str(c["rep"]).strip().lower()
    guardar_secuencias(limpio)
    return limpio


@app.get("/api/historial_todo")
def historial_todo():
    """{"lista_id|clave": [cambios de estado]} de todas las empresas: alimenta la gráfica de avance diario.
    Es lo mismo que publica Vercel en historial.json."""
    return publicar_vercel._historial_por_empresa()


@app.get("/api/historial")
def historial(lista_id: str, clave: str):
    if not lista_id or not clave:
        raise HTTPException(400, "lista_id y clave son obligatorios")
    return motor.historial(lista_id, clave)


@app.get("/api/gestiones")
def ver_gestiones():
    """Vistas ejecutiva y operativa armadas desde el último dato (lo mismo que gestiones.json en Vercel)."""
    if not (RAIZ / "datos" / "ultimo.json").exists():
        raise HTTPException(404, "Todavía no hay una sincronización")
    return gestiones.cargar()


@app.get("/")
def inicio():
    return FileResponse(RAIZ / "static" / "index.html")


@app.get("/gestiones.html")
def pagina_gestiones():
    return FileResponse(RAIZ / "static" / "gestiones.html")


app.mount("/static", StaticFiles(directory=RAIZ / "static"), name="static")
