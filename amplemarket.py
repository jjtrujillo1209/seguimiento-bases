"""Lectura de Amplemarket: usuarios y listas de leads que siguen la convención de base semanal.

La API pública no expone los leads de una secuencia, así que la fuente de la base
es la lista de leads: una por rep y por semana, con nombre «Base S45 · Andrea Jaramillo».
"""
import json
import re
import time
import unicodedata
from datetime import date
from pathlib import Path

import requests

BASE_URL = "https://api.amplemarket.com"
RAIZ = Path(__file__).parent

# «Base S45 · Andrea Jaramillo», «Base Semana 45 2026 - Andrea», «BASE S7 | baldovino@platzi.com»
PATRON_BASE = re.compile(
    r"^\s*base\s+s(?:emana)?\s*(\d{1,2})(?:\s*[-/]?\s*(\d{4}))?\s*[·|\-–—:]\s*(.+?)\s*$",
    re.IGNORECASE,
)

DOMINIOS_GENERICOS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.es", "live.com",
    "icloud.com", "hotmail.es", "outlook.es", "msn.com", "protonmail.com", "aol.com",
}


# Estados que acepta el filtro de `/tasks` → columna del progreso por etapa (la tarea llega con
# `status` scheduled para due y upcoming)
ESTADOS_TAREA = ("due", "upcoming", "completed", "skipped", "paused", "cancelled")
COLUMNA_TAREA = {"scheduled": "programadas", "completed": "completadas", "skipped": "saltadas",
                 "paused": "pausadas", "cancelled": "canceladas"}


def progreso_por_etapa(tareas: list[dict]) -> list[dict]:
    """Progreso por etapa de una secuencia, como la tabla «Progress» de Amplemarket, a partir de sus
    tareas. La API no trae el número de etapa: la etapa es el tipo de tarea y, si el tipo se repite
    en la secuencia, su ocurrencia para cada contacto (la 2.ª llamada de un contacto es la 2.ª etapa
    de llamada). El id de la tarea crece con su creación, así que ordena las etapas. Cada fila trae
    el total y el desglose por usuario (`por_usuario`). Los correos automáticos no son tareas."""
    por_contacto: dict[str, list[dict]] = {}
    for t in tareas:
        por_contacto.setdefault((t.get("contact") or {}).get("id") or t["id"], []).append(t)
    etapas: dict[tuple, dict] = {}
    for ts in por_contacto.values():
        vistos: dict[str, int] = {}
        for pos, t in enumerate(sorted(ts, key=lambda t: t["id"])):
            columna = COLUMNA_TAREA.get(t.get("status"))
            if not columna:
                continue
            vistos[t["type"]] = n = vistos.get(t["type"], 0) + 1
            e = etapas.setdefault((t["type"], n), {
                "tipo": t["type"], "n": n, "auto": 0, "total": 0, "_pos": 0,
                **{c: 0 for c in COLUMNA_TAREA.values()}, "por_usuario": {},
            })
            u = e["por_usuario"].setdefault((t.get("user_email") or "").lower(), {c: 0 for c in COLUMNA_TAREA.values()})
            e[columna] += 1
            u[columna] += 1
            e["total"] += 1
            e["auto"] += bool(t.get("automatic"))
            e["_pos"] += pos
    filas = sorted(etapas.values(), key=lambda e: (e["_pos"] / e["total"], e["tipo"], e["n"]))
    for e in filas:
        e["automatica"] = e.pop("auto") * 2 > e["total"]
        del e["_pos"], e["total"]
    return filas


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texto).strip().lower()


def normalizar_dominio(valor: str | None) -> str:
    valor = (valor or "").strip().lower()
    valor = re.sub(r"^https?://", "", valor)
    valor = re.sub(r"^www\.", "", valor)
    return valor.split("/")[0].split("?")[0]


class Amplemarket:
    def __init__(self, api_key: str):
        self.sesion = requests.Session()
        self.sesion.headers["Authorization"] = f"Bearer {api_key}"

    def _get(self, ruta: str) -> dict:
        for intento in range(6):
            r = self.sesion.get(BASE_URL + ruta, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 + intento * 2)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def _paginar(self, ruta: str, clave: str) -> list[dict]:
        filas, siguiente = [], f"{ruta}?page[size]=100"
        while siguiente:
            d = self._get(siguiente)
            filas += d.get(clave, [])
            siguiente = (d.get("_links", {}).get("next") or {}).get("href")
        return filas

    def usuarios(self) -> list[dict]:
        return self._paginar("/users", "users")

    def listas(self) -> list[dict]:
        return self._paginar("/lead-lists", "lead_lists")

    def secuencias(self) -> list[dict]:
        return self._paginar("/sequences", "sequences")

    def tareas(self, user_id: str, secuencia_id: str) -> list[dict]:
        """Tareas de un usuario en una secuencia (API pública `/tasks`), en todos sus estados.
        El filtro `status` solo acepta un valor y sin él responde solo las pendientes; la página
        trae 20 como máximo. Un usuario que no admite tareas responde 400 «Invalid user»."""
        filas = []
        for estado in ESTADOS_TAREA:
            siguiente = f"/tasks?user_id={user_id}&sequence_key={secuencia_id}&status={estado}&page[size]=100"
            while siguiente:
                try:
                    d = self._get(siguiente)
                except requests.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code == 400:
                        return filas
                    raise
                filas += d.get("tasks", [])
                siguiente = (d.get("_links", {}).get("next") or {}).get("href")
        return filas

    def lista(self, lista_id: str) -> dict:
        """Detalle de la lista con TODOS sus leads (vienen paginados de a 100)."""
        detalle = self._get(f"/lead-lists/{lista_id}?page[size]=100")
        siguiente = (detalle.get("_links", {}).get("next") or {}).get("href")
        while siguiente:
            pagina = self._get(siguiente)
            detalle["leads"] += pagina.get("leads", [])
            siguiente = (pagina.get("_links", {}).get("next") or {}).get("href")
        return detalle


def resolver_rep(texto: str, usuarios: list[dict]) -> dict | None:
    """Encuentra al usuario de Amplemarket por email, nombre completo o un nombre único."""
    t = normalizar(texto)
    for u in usuarios:
        if t == normalizar(u.get("email")) or t == normalizar(u.get("name")):
            return u
    candidatos = [u for u in usuarios if u.get("name") and t in normalizar(u["name"])]
    if len(candidatos) == 1:
        return candidatos[0]
    candidatos = [u for u in usuarios if t and t in normalizar(u.get("email", "").split("@")[0])]
    return candidatos[0] if len(candidatos) == 1 else None


def lunes_iso(anio: int, semana: int) -> date:
    return date.fromisocalendar(anio, semana, 1)


def bases_manuales() -> dict:
    """datos/bases_manual.json: {lista_id: {"rep": email, "semana": 45, "anio": 2026}}.
    Sirve para listas que no siguen la convención de nombre."""
    ruta = RAIZ / "datos" / "bases_manual.json"
    return json.loads(ruta.read_text()) if ruta.exists() else {}


def detectar_bases(listas: list[dict], usuarios: list[dict]) -> tuple[list[dict], list[dict]]:
    """Devuelve (bases, avisos). Cada base: lista + rep + semana ISO + fecha de inicio."""
    manuales, bases, avisos = bases_manuales(), [], []
    for l in listas:
        anio_creada = int(l["created_at"][:4])
        if l["id"] in manuales:
            m = manuales[l["id"]]
            rep_txt, semana, anio = m["rep"], int(m["semana"]), int(m.get("anio", anio_creada))
        else:
            coincide = PATRON_BASE.match(l.get("name") or "")
            if not coincide:
                continue
            semana = int(coincide.group(1))
            anio = int(coincide.group(2) or anio_creada)
            rep_txt = coincide.group(3)
        rep = resolver_rep(rep_txt, usuarios)
        if not rep:
            rep = resolver_rep(l.get("owner", ""), usuarios)
            avisos.append({
                "lista": l["name"],
                "aviso": f"No reconocí al rep «{rep_txt}»; usé al dueño de la lista ({l.get('owner')}).",
            })
        try:
            inicio = lunes_iso(anio, semana)
        except ValueError:
            avisos.append({"lista": l["name"], "aviso": f"Semana {semana} inválida; lista ignorada."})
            continue
        bases.append({
            "lista_id": l["id"],
            "lista_nombre": l["name"],
            "lista_url": l.get("url"),
            "lista_creada": l["created_at"],
            "semana": semana,
            "anio": anio,
            "semana_clave": f"{anio}-S{semana:02d}",
            "inicio": inicio.isoformat(),
            "rep_email": (rep or {}).get("email", l.get("owner")),
            "rep_nombre": (rep or {}).get("name") or (rep or {}).get("email") or l.get("owner"),
        })
    return bases, avisos


def empresas_de_lista(detalle: dict) -> list[dict]:
    """Agrupa los leads de una lista de leads por empresa."""
    return agrupar_empresas([{
        "id": lead.get("id"),
        "email": lead.get("email"),
        "nombre": " ".join(x for x in [lead.get("first_name"), lead.get("last_name")] if x),
        "cargo": lead.get("title"),
        "linkedin": lead.get("linkedin_url") or lead.get("linkedin") or None,
        "validacion": (lead.get("email_validation_result") or {}).get("result"),
        "empresa": lead.get("company_name"),
        "dominio_empresa": lead.get("company_domain"),
    } for lead in detalle.get("leads", [])])


def lead_de_secuencia(lead: dict, secuencia: dict) -> dict:
    """Normaliza un lead de la API interna de secuencias (ver amplemarket_ui.py)."""
    # Los borradores solo traen dynamic_fields (lo que se importó) y readiness
    cuenta = lead.get("prospect_hub_account") or {}
    campos = lead.get("dynamic_fields") or {}
    eng = lead.get("engagement") or {}
    paso = lead.get("current_sequence_stage") or {}
    nombre = lead.get("full_name") or " ".join(x for x in [campos.get("first_name"), campos.get("last_name")] if x)
    return {
        "id": lead.get("id") or lead.get("key"),
        "email": lead.get("email") or campos.get("email"),
        "nombre": nombre or campos.get("decisor"),
        "cargo": campos.get("cargo"),
        "linkedin": lead.get("linkedin_url") or campos.get("linkedin_url") or campos.get("linkedin"),
        "validacion": lead.get("email_status"),
        "empresa": cuenta.get("name") or campos.get("empresa"),
        "dominio_empresa": cuenta.get("domain"),
        "am": {
            "secuencia_id": secuencia["id"],
            "secuencia": secuencia["name"],
            "estado": lead.get("status") or "draft",
            "paso": f"{paso.get('label')} · {paso.get('type')}" if paso else None,
            "inicio": lead.get("start_date"),
            "primer_contacto": lead.get("first_contact_date"),
            "aperturas": eng.get("number_of_opens") or 0,
            "respondio": bool(eng.get("replied")),
            "llamada_agendada": bool(eng.get("scheduled_a_call")),
            "motivo": lead.get("completed_reason") or lead.get("removed_reason") or lead.get("error_reason")
                      or lead.get("paused_reason"),
            "buzon": lead.get("mailbox"),
        },
    }


def agrupar_empresas(leads: list[dict]) -> list[dict]:
    """Agrupa leads normalizados por empresa (dominio, si no por nombre)."""
    empresas: dict[str, dict] = {}
    for lead in leads:
        email = (lead.get("email") or "").strip().lower()
        dominio = normalizar_dominio(lead.get("dominio_empresa"))
        if not dominio and "@" in email:
            dominio_email = email.split("@")[1]
            if dominio_email not in DOMINIOS_GENERICOS:
                dominio = dominio_email
        nombre = (lead.get("empresa") or "").strip()
        clave = dominio or f"nombre:{normalizar(nombre)}"
        if clave == "nombre:":
            clave = f"lead:{lead.get('id')}"
        e = empresas.setdefault(clave, {
            "clave": clave, "nombre": nombre or dominio, "dominio": dominio, "leads": [],
        })
        if not e["nombre"] and nombre:
            e["nombre"] = nombre
        e["leads"].append({k: v for k, v in lead.items() if k not in ("empresa", "dominio_empresa", "id")}
                          | {"email": email})
    return list(empresas.values())


def secuencias_seguidas() -> dict:
    """datos/secuencias.json: {secuencia_id: {"semana": 45, "anio": 2026}}."""
    ruta = RAIZ / "datos" / "secuencias.json"
    return json.loads(ruta.read_text()) if ruta.exists() else {}


def guardar_secuencias(config: dict) -> None:
    (RAIZ / "datos" / "secuencias.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))


def bases_de_secuencias(secuencias: list[dict], leads_por_secuencia: dict[str, list[dict]],
                        config: dict, usuarios: list[dict]) -> list[dict]:
    """Una base por (semana, rep). Si la secuencia tiene rep fijo en la configuración, todos sus
    leads son de ese rep; si no, el rep de cada lead es su owner en Amplemarket, así una
    secuencia compartida (p. ej. Carlos y Nata) se reparte bien."""
    por_id = {s["id"]: s for s in secuencias}
    nombres = {(u.get("email") or "").lower(): u.get("name") for u in usuarios}
    grupos: dict[tuple, dict] = {}
    for sid, conf in config.items():
        s = por_id.get(sid)
        if not s:
            continue
        semana, anio = int(conf["semana"]), int(conf["anio"])
        rep_fijo = (conf.get("rep") or "").lower()
        for crudo in leads_por_secuencia.get(sid, []):
            owner = crudo.get("owner") or {}
            # respaldo: el campo «responsable» que viene en el CSV importado
            responsable = ((crudo.get("dynamic_fields") or {}).get("responsable") or "").strip().lower()
            rep_email = rep_fijo or (owner.get("email") or "").lower() or responsable or "sin-owner"
            nombre = owner.get("name") if owner.get("email") and not rep_fijo else nombres.get(rep_email)
            g = grupos.setdefault((anio, semana, rep_email), {
                "rep_nombre": nombre or rep_email, "leads": [], "secuencias": {},
            })
            g["leads"].append(lead_de_secuencia(crudo, s))
            g["secuencias"][sid] = s["name"]
    bases = []
    for (anio, semana, rep_email), g in grupos.items():
        bases.append({
            "fuente": "secuencias",
            "lista_id": f"sec:{anio}-S{semana:02d}:{rep_email}",
            "lista_nombre": " · ".join(g["secuencias"].values()),
            "lista_url": None,
            "secuencias": [{"id": i, "nombre": n, "url": f"https://app.amplemarket.com/dashboard/sequences/{i}"}
                           for i, n in g["secuencias"].items()],
            "semana": semana,
            "anio": anio,
            "semana_clave": f"{anio}-S{semana:02d}",
            "inicio": lunes_iso(anio, semana).isoformat(),
            "rep_email": rep_email,
            "rep_nombre": g["rep_nombre"],
            "empresas": agrupar_empresas(g["leads"]),
        })
    return bases
