"""Equipo comercial: sellers, segmento, equipo, región y líder (equipo.json).

Es la fuente del segmento de cada base. Se aplica al leer los datos (api.py y publicar_vercel.py),
así que cambiar a un seller de segmento o de líder no exige volver a sincronizar.
La sincronización también lo usa para seguir sola las secuencias que crean los sellers.
"""
import json
from pathlib import Path

RUTA = Path(__file__).parent / "equipo.json"
SEGMENTOS = {"smb": "SMB", "mid_market": "Mid Market", "corporate": "Corporate", "gobierno": "Gobierno",
             "sdr_ent": "SDR ENT"}
SIN_SEGMENTO = "sin_segmento"


def cargar_equipo() -> list[dict]:
    try:
        crudo = json.loads(RUTA.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    sellers = []
    for s in crudo.get("sellers", []):
        email = (s.get("email") or "").strip().lower()
        if not email:
            continue
        sellers.append({"email": email, "nombre": s.get("nombre") or email,
                        "segmento": s.get("segmento") if s.get("segmento") in SEGMENTOS else SIN_SEGMENTO,
                        "equipo": s.get("equipo"), "region": s.get("region"), "lider": s.get("lider")})
    return sellers


def por_email() -> dict[str, dict]:
    return {s["email"]: s for s in cargar_equipo()}


def aplicar(datos: dict) -> dict:
    """Marca cada base con el segmento, equipo, región y líder de su seller, y adjunta el equipo."""
    equipo = cargar_equipo()
    mapa = {s["email"]: s for s in equipo}
    for b in datos.get("bases", []):
        s = mapa.get((b.get("rep_email") or "").lower())
        b["segmento"] = s["segmento"] if s else SIN_SEGMENTO
        b["equipo"] = s["equipo"] if s else None
        b["region"] = s["region"] if s else None
        b["lider"] = s["lider"] if s else None
        if s:
            b["rep_nombre"] = s["nombre"]
    datos["segmentos"] = SEGMENTOS
    datos["equipo"] = equipo
    return datos
