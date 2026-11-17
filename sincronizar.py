"""Sincroniza las bases semanales de Amplemarket con su tratamiento en HubSpot.

Uso directo:  .venv/bin/python sincronizar.py
Escribe datos/ultimo.json (lo que lee la UI), una copia en datos/snapshots/
y una línea por empresa en datos/historial.jsonl para la trazabilidad de estados.
"""
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from amplemarket import (DOMINIOS_GENERICOS, Amplemarket, bases_de_secuencias, detectar_bases,
                         empresas_de_lista, lead_de_secuencia, lunes_iso, normalizar, progreso_por_etapa,
                         secuencias_seguidas)
from amplemarket_ui import leads_de_secuencias
from hubspot import (PROPS_ACTIVIDAD, PROPS_CONTACTO, PROPS_DEAL, PROPS_EMPRESA, PROPS_LEAD, HubSpot)
import segmentos

RAIZ = Path(__file__).parent
DATOS = RAIZ / "datos"
load_dotenv(RAIZ / ".env")

LLAMADA_CONECTADA = "f240bbac-87c9-4f6e-bf70-924b57d47db7"  # disposición «Connected» por defecto

# Orden = avance en el embudo. La empresa queda en el más alto que alcanzó.
ESTADOS = ["no_en_hubspot", "sin_tocar", "contactada", "respondio", "reunion", "deal"]


# Un toque previo a la base cuenta como «tratada antes» si fue en estos días antes del lunes de inicio
DIAS_PREVIO_RECIENTE = 90

# Tratamiento de un contacto en HubSpot, de menor a mayor avance
NIVELES_HS = ["no_en_hubspot", "contacto", "prospecto", "lead", "deal"]
ETAPAS_LEAD = {"lead", "marketingqualifiedlead", "salesqualifiedlead"}
ETAPAS_DEAL = {"opportunity", "customer"}


def _semana_iso(fecha_iso: str) -> tuple[int, int]:
    anio, semana, _ = datetime.fromisoformat(fecha_iso.replace("Z", "+00:00")).isocalendar()
    return anio, semana


# Amplemarket crea 5 borradores de ejemplo al dar de alta a un usuario: estas dos plantillas y tres
# generadas por IA, todos en el mismo minuto. Un borrador creado a ±10 min de una plantilla es de ese lote.
TAG_SEGUIMIENTO = os.getenv("TAG_SEGUIMIENTO", "seller_full_stack")
PLANTILLAS_AM = {"branched multi-channel sequence", "getting started with ai copy"}


def _de_ejemplo(secuencias: list[dict]) -> set[str]:
    def ts(s):
        try:
            return datetime.fromisoformat((s.get("created_at") or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    anclas: dict[str, list] = {}
    for s in secuencias:
        if (s.get("name") or "").strip().lower() in PLANTILLAS_AM and ts(s):
            anclas.setdefault((s.get("created_by_user_email") or "").lower(), []).append(ts(s))
    ejemplo = set()
    for s in secuencias:
        t, creador = ts(s), (s.get("created_by_user_email") or "").lower()
        if s.get("status") == "draft" and t and any(abs((t - a).total_seconds()) <= 600 for a in anclas.get(creador, [])):
            ejemplo.add(s["id"])
    return ejemplo


def _secuencias_automaticas(secuencias: list[dict], config: dict, equipo: dict, corte: str) -> dict:
    """Secuencias creadas por un seller del equipo en las semanas activas que aún no están configuradas.
    Se siguen solas con el creador como rep y la semana ISO de creación. Las que no tienen leads no
    generan base (los borradores de ejemplo de Amplemarket quedan fuera así)."""
    auto, ejemplo = {}, _de_ejemplo(secuencias)
    for s in secuencias:
        creador = (s.get("created_by_user_email") or "").lower()
        creada = s.get("created_at") or ""
        # También las marcadas en Amplemarket con el tag de seguimiento, las cree quien las cree: si el
        # creador no es un seller, quedan sin rep fijo y sus leads se reparten por owner
        marcada = TAG_SEGUIMIENTO in (s.get("tags") or [])
        if s["id"] in config or not (creador in equipo or marcada) or not creada or creada[:10] < corte:
            continue
        if s.get("status") == "archived" or s["id"] in ejemplo:
            continue
        anio, semana = _semana_iso(creada)
        auto[s["id"]] = {"semana": semana, "anio": anio, "auto": True} | ({"rep": creador} if creador in equipo else {})
    return auto


def _inventario_secuencias(secuencias, config, leads, equipo, corte) -> list[dict]:
    """Todas las secuencias de los sellers del equipo: las que crearon y las asignadas a ellos."""
    filas, ejemplo = [], _de_ejemplo(secuencias)
    for s in secuencias:
        if s["id"] in ejemplo and s["id"] not in config:
            continue
        conf = config.get(s["id"])
        creador = (s.get("created_by_user_email") or "").lower()
        rep = ((conf or {}).get("rep") or "").lower() or (creador if creador in equipo else None)
        compartida = conf is not None and not (conf or {}).get("rep")
        if not (rep in equipo or compartida):
            continue
        if not conf and (s.get("created_at") or "")[:10] < corte:
            continue
        crudos = leads.get(s["id"])
        normal = [lead_de_secuencia(c, s)["am"] for c in crudos] if crudos is not None else None
        # en una compartida, quienes tienen leads en ella (el owner de cada lead), estén o no en equipo.json:
        # así la secuencia de alguien fuera del equipo (p. ej. una líder) sale a su nombre
        reps_con_leads = sorted({((c.get("owner") or {}).get("email") or "").lower() for c in crudos or []} - {""}) \
            if compartida else []
        semana = f"{conf['anio']}-S{int(conf['semana']):02d}" if conf else None
        filas.append({
            "id": s["id"], "nombre": s.get("name"), "estado": s.get("status"),
            "creador": creador, "rep": rep if rep in equipo else None, "compartida": compartida, "reps": reps_con_leads,
            "creada": s.get("created_at"), "actualizada": s.get("updated_at"),
            "url": f"https://app.amplemarket.com/dashboard/sequences/{s['id']}",
            "seguimiento": ("auto" if conf.get("auto") else "manual") if conf else "no",
            "semana_clave": semana,
            "leads": len(normal) if normal is not None else None,
            "contactados": sum(bool(x["primer_contacto"]) for x in normal) if normal is not None else None,
            "respondieron": sum(x["respondio"] for x in normal) if normal is not None else None,
            "por_iniciar": sum(x["estado"] in ("ready", "draft") for x in normal) if normal is not None else None,
        })
    return sorted(filas, key=lambda f: f["creada"] or "", reverse=True)


def _progreso_secuencias(am, inventario, leads, usuarios, anterior, paso) -> None:
    """Agrega a cada secuencia del inventario `etapas` (progreso por etapa, de las tareas de la API
    pública) y `pasos_leads` (en qué paso está hoy cada lead en curso). Las tareas son de quien las
    ejecuta, que puede no ser quien creó la secuencia ni el owner del lead, y la API obliga a pedirlas
    usuario por usuario: una vez al día (y la primera vez) se recorren todos los usuarios y se anota
    quiénes tienen tareas (`tareas_usuarios`); el resto del día se consulta a esos, a los owners de
    los leads, al creador y al rep. Las secuencias que no se leyeron conservan su último dato."""
    ids = {(u.get("email") or "").lower(): u["id"] for u in usuarios}
    previas = {s["id"]: s for s in anterior.get("secuencias", [])}
    vigentes = [f for f in inventario if f["id"] in leads]
    if vigentes:
        paso(f"Leyendo el progreso por etapa de {len(vigentes)} secuencias…")
    hoy = date.today().isoformat()
    for f in inventario:
        previa = previas.get(f["id"], {})
        for k in ("etapas", "pasos_leads", "tareas_usuarios", "tareas_revisadas"):
            f[k] = previa.get(k)
        if f["id"] not in leads:
            continue
        crudos = leads[f["id"]]
        pasos = {}
        for c in crudos:
            p = c.get("current_sequence_stage") or {}
            if p and c.get("status") == "in_sequence":
                clave = (str(p.get("label") or ""), p.get("type"))
                pasos[clave] = pasos.get(clave, 0) + 1
        f["pasos_leads"] = [{"paso": k[0], "tipo": k[1], "leads": n} for k, n in sorted(pasos.items())]
        completa = f["tareas_revisadas"] != hoy
        emails = set(ids) if completa else (
            {((c.get("owner") or {}).get("email") or "").lower() for c in crudos}
            | {f["creador"], f["rep"]} | set(f["tareas_usuarios"] or []))
        try:
            tareas = [t for e in sorted(x for x in emails if x in ids) for t in am.tareas(ids[e], f["id"])]
            f["etapas"] = progreso_por_etapa(tareas)
            f["tareas_usuarios"] = sorted({(t.get("user_email") or "").lower() for t in tareas})
            if completa:
                f["tareas_revisadas"] = hoy
        except Exception as exc:  # el progreso es un extra: no tumba la sincronización
            paso(f"  Sin progreso por etapa para «{f['nombre']}»: {str(exc)[:120]}")


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fecha(valor: str | None) -> str | None:
    """Normaliza timestamps de HubSpot (ISO o epoch ms) a ISO UTC con segundos."""
    if not valor:
        return None
    try:
        if valor.isdigit():
            dt = datetime.fromtimestamp(int(valor) / 1000, timezone.utc)
        else:
            dt = datetime.fromisoformat(valor.replace("Z", "+00:00"))
            dt = dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)
    except ValueError:
        return None
    return dt.isoformat(timespec="seconds")


# Correos entrantes que no son una respuesta real del prospecto
NO_ES_RESPUESTA = re.compile(
    r"^\s*(accepted|declined|tentative|updated invitation|invitation|aceptad[oa]|rechazad[oa]|"
    r"provisional|invitaci[oó]n|automatic reply|auto(matic)?[- ]?reply|respuesta autom[aá]tica|"
    r"out of (the )?office|fuera de (la )?oficina|undeliverable|delivery status|no se pudo entregar)",
    re.IGNORECASE,
)

# Sufijos de país intercambiables para la búsqueda aproximada (aserta.com ↔ aserta.com.mx)
SUFIJOS = [".com.mx", ".com.co", ".com.ar", ".com.pe", ".com.br", ".com.ec", ".com.uy", ".com.gt",
           ".com", ".mx", ".co", ".cl", ".pe", ".ar", ".ec", ".net", ".org", ".io", ".lat"]


def _variantes(dominio: str) -> set[str]:
    for s in SUFIJOS:
        if dominio.endswith(s):
            raiz = dominio[: -len(s)]
            if "." in raiz or len(raiz) < 4:  # subdominios o raíces cortas: demasiado ambiguo
                return set()
            return {raiz + o for o in SUFIJOS if o != s}
    return set()


def _dominios_exactos(e: dict) -> set[str]:
    dominios = {e["dominio"]} if e["dominio"] else set()
    for l in e["leads"]:
        if "@" in (l["email"] or ""):
            d = l["email"].split("@")[1]
            if d not in DOMINIOS_GENERICOS:
                dominios.add(d)
    return dominios


def _entero(valor) -> int:
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return 0


def sincronizar(avisar=None) -> dict:
    paso = avisar or (lambda m: print(m, flush=True))
    am_key = os.getenv("AMPLEMARKET_API_KEY", "").strip()
    hs_token = os.getenv("HUBSPOT_PRIVATE_APP_TOKEN", "").strip()
    if not am_key:
        raise RuntimeError("Falta AMPLEMARKET_API_KEY en .env")
    if not hs_token:
        raise RuntimeError("Falta HUBSPOT_PRIVATE_APP_TOKEN en .env")
    semanas_activas = int(os.getenv("SEMANAS_ACTIVAS", "8"))

    am, hs = Amplemarket(am_key), HubSpot(hs_token)
    hs.verificar()

    paso("Leyendo usuarios y listas de Amplemarket…")
    usuarios = am.usuarios()
    bases, avisos = detectar_bases(am.listas(), usuarios)

    anterior = _leer_json(DATOS / "ultimo.json") or {}
    previas = {b["lista_id"]: b for b in anterior.get("bases", [])}
    corte = (date.today() - timedelta(weeks=semanas_activas)).isoformat()

    # Bases por secuencia: se vuelven a leer las de semanas activas y, sin importar su edad, las
    # secuencias que siguen activas en Amplemarket (p. ej. «Pato 05/07», semana 19)
    # Además de las configuradas a mano, se siguen solas las que crean los sellers de equipo.json
    equipo = segmentos.por_email()
    todas_secuencias = am.secuencias()
    config = secuencias_seguidas()
    config |= _secuencias_automaticas(todas_secuencias, config, equipo, corte)
    en_curso = {s["id"] for s in todas_secuencias if s.get("status") == "active"}
    vigentes = {sid: c for sid, c in config.items()
                if lunes_iso(int(c["anio"]), int(c["semana"])).isoformat() >= corte or sid in en_curso}
    leads = {}
    if vigentes:
        paso(f"Leyendo leads de {len(vigentes)} secuencias en el Chrome dedicado…")
        leads = leads_de_secuencias(list(vigentes))
        for sid, filas in leads.items():
            paso(f"  {len(filas)} leads en la secuencia {sid[:8]}…")
        bases += bases_de_secuencias(todas_secuencias, leads, vigentes, usuarios)
    inventario = _inventario_secuencias(todas_secuencias, config, leads, equipo, corte)
    _progreso_secuencias(am, inventario, leads, usuarios, anterior, paso)
    vistas = {b["lista_id"] for b in bases}
    bases_viejas = [b | {"congelada": True} for i, b in previas.items()
                    if b.get("fuente") == "secuencias" and i not in vistas and b["inicio"] < corte]

    sigue_activa = lambda b: any(x["id"] in en_curso for x in b.get("secuencias", []))
    activas = [b for b in bases if b["inicio"] >= corte or b["lista_id"] not in previas or sigue_activa(b)]
    congeladas = [previas[b["lista_id"]] | {"congelada": True} for b in bases if b not in activas] + bases_viejas

    paso(f"{len(activas) + len(congeladas)} bases ({len(activas)} a sincronizar, {len(congeladas)} congeladas).")
    for b in activas:
        if "empresas" not in b:
            paso(f"Leyendo lista «{b['lista_nombre']}»…")
            b["empresas"] = empresas_de_lista(am.lista(b["lista_id"]))

    paso("Leyendo owners y etapas de HubSpot…")
    owners = {o["id"]: o for o in hs.owners()}
    owner_por_email = {(o.get("email") or "").lower(): o["id"] for o in owners.values()}
    etapas = hs.etapas_deal()
    portal = hs.portal_id()
    etiquetas = {
        "etapa": hs.etiquetas("companies", "lifecyclestage"),
        "lead_status": hs.etiquetas("companies", "hs_lead_status") | hs.etiquetas("contacts", "hs_lead_status"),
        "etapa_lead": hs.etapas_lead(),
    }

    _cruzar_con_hubspot(hs, activas, owners, owner_por_email, etapas, paso, etiquetas["etapa_lead"])
    _marcar_asignacion_doble(activas)

    resultado = {
        "generado": _ahora(),
        "portal_id": portal,
        "etiquetas": etiquetas,
        "avisos": avisos,
        "secuencias": inventario,
        "bases": sorted(activas + congeladas, key=lambda b: (b["inicio"], b["rep_nombre"] or ""), reverse=True),
    }
    _guardar(resultado)
    paso("Listo.")
    return resultado


def _cruzar_con_hubspot(hs, bases, owners, owner_por_email, etapas, paso, etapas_lead=None):
    todas = [e for b in bases for e in b["empresas"]]
    for e in todas:
        e["dominios"] = sorted(_dominios_exactos(e))
    dominios = {d for e in todas for d in e["dominios"]}
    emails = [l["email"] for e in todas for l in e["leads"] if l["email"]]

    paso(f"Buscando {len(dominios)} dominios y {len(set(emails))} contactos en HubSpot…")
    empresas_hs = hs.buscar_en("companies", "domain", list(dominios), PROPS_EMPRESA)
    contactos_hs = hs.buscar_en("contacts", "email", emails, PROPS_CONTACTO)
    por_dominio: dict[str, list[dict]] = {}
    for c in empresas_hs:
        por_dominio.setdefault((c["properties"].get("domain") or "").lower(), []).append(c)
    por_email = {(c["properties"].get("email") or "").lower(): c for c in contactos_hs}

    # Segunda pasada, solo para las que no aparecieron: variantes de país y nombre exacto
    def encontrada(e):
        return any(por_dominio.get(d) for d in e["dominios"]) or any(por_email.get(l["email"]) for l in e["leads"])
    pendientes = [e for e in todas if not encontrada(e)]
    variantes = {v for e in pendientes for d in e["dominios"] for v in _variantes(d)} - dominios
    nombres = {e["nombre"] for e in pendientes if e["nombre"] and len(normalizar(e["nombre"])) >= 4}
    paso(f"Búsqueda aproximada para {len(pendientes)} empresas no encontradas…")
    for c in hs.buscar_en("companies", "domain", list(variantes), PROPS_EMPRESA):
        por_dominio.setdefault((c["properties"].get("domain") or "").lower(), []).append(c)
        empresas_hs.append(c)
    por_nombre: dict[str, list[dict]] = {}
    for c in hs.buscar_en("companies", "name", list(nombres), PROPS_EMPRESA):
        por_nombre.setdefault(normalizar(c["properties"].get("name")), []).append(c)
        empresas_hs.append(c)

    # Empresas a las que llegamos por el contacto aunque el dominio no coincida
    ids_empresa_de_contacto = [c["properties"].get("associatedcompanyid") for c in contactos_hs]
    extra = hs.leer("companies", [i for i in ids_empresa_de_contacto if i], PROPS_EMPRESA)

    ids_empresas = {c["id"] for c in empresas_hs} | set(extra)
    ids_contactos = {c["id"] for c in contactos_hs}

    paso("Leyendo actividades y deals asociados…")
    ids_actividad: dict[str, set] = {t: set() for t in PROPS_ACTIVIDAD}
    asoc: dict[tuple, dict] = {}
    for origen, ids in (("companies", ids_empresas), ("contacts", ids_contactos)):
        for tipo in list(PROPS_ACTIVIDAD) + ["deals"]:
            asoc[(origen, tipo)] = hs.asociaciones(origen, tipo, list(ids)) if ids else {}
            if tipo in ids_actividad:
                for lista in asoc[(origen, tipo)].values():
                    ids_actividad[tipo].update(lista)

    actividades = {}
    for tipo, ids in ids_actividad.items():
        paso(f"Leyendo {len(ids)} {tipo}…")
        for i, p in hs.leer(tipo, list(ids), PROPS_ACTIVIDAD[tipo]).items():
            actividades[(tipo, i)] = p
    ids_deals = {d for (o, t), m in asoc.items() if t == "deals" for l in m.values() for d in l}
    deals = hs.leer("deals", list(ids_deals), PROPS_DEAL)

    # Objeto Leads de HubSpot por contacto (si el portal no lo expone, el contacto se mide por su etapa)
    leads_por_contacto: dict[str, list[dict]] = {}
    if ids_contactos and etapas_lead:
        try:
            asoc_leads = hs.asociaciones("contacts", "leads", list(ids_contactos))
            objetos = hs.leer("leads", list({x for v in asoc_leads.values() for x in v}), PROPS_LEAD)
            for cid, lids in asoc_leads.items():
                leads_por_contacto[cid] = [{
                    "id": i, "nombre": objetos[i].get("hs_lead_name"),
                    "etapa": etapas_lead.get(objetos[i].get("hs_pipeline_stage") or "", objetos[i].get("hs_pipeline_stage")),
                    "creado": _fecha(objetos[i].get("hs_createdate")),
                } for i in lids if i in objetos]
            paso(f"{sum(map(len, leads_por_contacto.values()))} leads de HubSpot asociados a los contactos.")
        except RuntimeError as exc:
            paso(f"No pude leer el objeto Leads de HubSpot ({str(exc)[:120]}); sigo con la etapa del contacto.")

    def nombre_owner(oid):
        o = owners.get(str(oid or ""))
        if not o:
            return None
        return " ".join(x for x in [o.get("firstName"), o.get("lastName")] if x) or o.get("email")

    for b in bases:
        rep_owner = owner_por_email.get((b["rep_email"] or "").lower())
        b["rep_owner_id"] = rep_owner
        inicio = b["inicio"]
        for e in b["empresas"]:
            candidatas = [c for d in e["dominios"] for c in por_dominio.get(d, [])]
            e["coincidencia"] = "dominio" if candidatas else None
            # completar con la empresa asociada de sus contactos
            for l in e["leads"]:
                c = por_email.get(l["email"])
                l["hs_id"] = c["id"] if c else None
                if c:
                    p = c["properties"]
                    l.update({
                        "hs_owner": nombre_owner(p.get("hubspot_owner_id")),
                        "hs_owner_id": p.get("hubspot_owner_id"),
                        "hs_etapa": p.get("lifecyclestage"),
                        "hs_lead_status": p.get("hs_lead_status"),
                        "hs_creado": _fecha(p.get("createdate")),
                        "hs_ultima_respuesta": _fecha(p.get("hs_sales_email_last_replied")),
                    })
                    cid = p.get("associatedcompanyid")
                    if cid and cid in extra and all(x["id"] != cid for x in candidatas):
                        candidatas.append({"id": cid, "properties": extra[cid]})
                        e["coincidencia"] = e["coincidencia"] or "contacto"
            # Sin cruce exacto: se sugieren posibles coincidencias, pero NO se usan para estado ni actividad
            e["posibles"] = []
            if not candidatas and not any(l.get("hs_id") for l in e["leads"]):
                posibles = [(c, "otro sufijo de dominio") for d in e["dominios"] for v in _variantes(d)
                            for c in por_dominio.get(v, [])]
                posibles += [(c, "nombre exacto") for c in por_nombre.get(normalizar(e["nombre"]), [])]
                vistos = set()
                for c, como in posibles:
                    if c["id"] not in vistos:
                        vistos.add(c["id"])
                        e["posibles"].append({"id": c["id"], "nombre": c["properties"].get("name"),
                                              "dominio": c["properties"].get("domain"), "como": como,
                                              "owner": nombre_owner(c["properties"].get("hubspot_owner_id"))})
            candidatas = list({c["id"]: c for c in candidatas}.values())
            _evaluar_empresa(e, candidatas, inicio, rep_owner, asoc, actividades, deals, etapas, nombre_owner)
            for l in e["leads"]:
                l["hs_trat"] = _tratamiento_contacto(l, inicio, rep_owner, asoc, actividades, deals, etapas,
                                                     leads_por_contacto)
        b["resumen"] = _resumir(b["empresas"])


def _tratamiento_contacto(l, inicio, rep_owner, asoc, actividades, deals, etapas, leads_por_contacto) -> dict:
    """Valida que un contacto trabajado desde Amplemarket tenga su tratamiento en HubSpot:
    existe, tiene actividad desde el inicio de la base y avanzó en Prospecto → Lead → Deal."""
    am = l.get("am") or {}
    trabajado = bool(am.get("primer_contacto"))
    respondio = bool(am.get("respondio"))
    hs_id = l.get("hs_id")
    t = {"nivel": "no_en_hubspot", "trabajado": trabajado, "respondio": respondio, "actividades": 0,
         "salientes": 0, "respuestas": 0, "ultima": None, "deals": [], "leads": [], "owner_ok": None, "brechas": []}
    if hs_id:
        acts = []
        for tipo in PROPS_ACTIVIDAD:
            for i in asoc[("contacts", tipo)].get(hs_id, []):
                p = actividades.get((tipo, i))
                fecha = p and _fecha(p.get("hs_timestamp") or p.get("hs_meeting_start_time") or p.get("hs_createdate"))
                if fecha and fecha[:10] >= inicio:
                    acts.append(_describir(tipo, i, p, fecha, rep_owner, lambda _: None))
        for i in asoc[("contacts", "deals")].get(hs_id, []):
            d = deals.get(i)
            if d:
                creado = _fecha(d.get("createdate"))
                t["deals"].append({"id": i, "nombre": d.get("dealname"),
                                   "etapa": etapas.get(d.get("dealstage") or "", {}).get("etiqueta", d.get("dealstage")),
                                   "nuevo": bool(creado and creado[:10] >= inicio),
                                   "cerrado": d.get("hs_is_closed") == "true", "ganado": d.get("hs_is_closed_won") == "true"})
        t["leads"] = leads_por_contacto.get(hs_id, [])
        etapa = (l.get("hs_etapa") or "").lower()
        if any(not d["cerrado"] or d["nuevo"] for d in t["deals"]) or etapa in ETAPAS_DEAL:
            t["nivel"] = "deal"
        elif t["leads"] or etapa in ETAPAS_LEAD:
            t["nivel"] = "lead"
        elif etapa == "subscriber":
            t["nivel"] = "prospecto"
        else:
            t["nivel"] = "contacto"
        t.update(actividades=len(acts), salientes=sum(a["saliente"] for a in acts),
                 respuestas=sum(a["respuesta"] for a in acts),
                 ultima=max((a["fecha"] for a in acts), default=None),
                 owner_ok=bool(rep_owner) and l.get("hs_owner_id") == rep_owner if rep_owner else None)
        if l.get("hs_ultima_respuesta") and l["hs_ultima_respuesta"][:10] >= inicio:
            t["respuestas"] = max(t["respuestas"], 1)
    if trabajado and not hs_id:
        t["brechas"].append({"tipo": "no_en_hubspot", "texto": "Amplemarket lo contactó y no existe en HubSpot"})
    elif trabajado and not t["salientes"]:
        t["brechas"].append({"tipo": "sin_actividad", "texto": "Amplemarket lo contactó y HubSpot no tiene la actividad"})
    if respondio and hs_id and t["nivel"] in ("contacto", "prospecto"):
        t["brechas"].append({"tipo": "sin_avance", "texto": "Respondió y en HubSpot sigue sin Lead ni Deal"})
    return t


def _marcar_asignacion_doble(bases):
    """La misma empresa asignada a más de un rep en la misma semana."""
    reps: dict[tuple, set] = {}
    for b in bases:
        for e in b["empresas"]:
            reps.setdefault((b["semana_clave"], e["clave"]), set()).add(b["rep_nombre"])
    for b in bases:
        for e in b["empresas"]:
            otros = sorted(reps[(b["semana_clave"], e["clave"])] - {b["rep_nombre"]})
            if otros:
                e["alertas"].append({"tipo": "asignacion_doble",
                                     "texto": f"También está en la base de {', '.join(otros)} esta semana"})
        b["resumen"]["con_alertas"] = sum(bool(e["alertas"]) for e in b["empresas"])


def _evaluar_empresa(e, candidatas, inicio, rep_owner, asoc, actividades, deals, etapas, nombre_owner):
    alertas = []
    # La principal: la del rep si existe, si no la más antigua (la «original»)
    candidatas.sort(key=lambda c: (c["properties"].get("hubspot_owner_id") != rep_owner,
                                   c["properties"].get("createdate") or ""))
    principal = candidatas[0] if candidatas else None
    contactos = [l for l in e["leads"] if l.get("hs_id")]

    if e.get("posibles"):
        lista_posibles = ", ".join(f"{p['nombre']} ({p['dominio'] or 'sin dominio'})" for p in e["posibles"][:3])
        alertas.append({"tipo": "coincidencia_aproximada",
                        "texto": f"Posible coincidencia en HubSpot, verificar: {lista_posibles}"})
    if len(candidatas) > 1:
        alertas.append({"tipo": "duplicada", "texto": f"{len(candidatas)} empresas en HubSpot con este dominio"})
    if principal:
        p = principal["properties"]
        e["hubspot"] = {
            "id": principal["id"], "nombre": p.get("name"), "dominio": p.get("domain"),
            "owner": nombre_owner(p.get("hubspot_owner_id")), "owner_id": p.get("hubspot_owner_id"),
            "etapa": p.get("lifecyclestage"), "lead_status": p.get("hs_lead_status"),
            "creada": _fecha(p.get("createdate")),
            "contactos_asociados": _entero(p.get("num_associated_contacts")),
            "otras": [c["id"] for c in candidatas[1:]],
        }
        if not p.get("hubspot_owner_id"):
            alertas.append({"tipo": "sin_owner", "texto": "La empresa no tiene owner en HubSpot"})
        elif rep_owner and p.get("hubspot_owner_id") != rep_owner:
            alertas.append({"tipo": "owner_distinto",
                            "texto": f"Owner en HubSpot: {e['hubspot']['owner'] or 'desconocido'}"})
        if (p.get("lifecyclestage") or "").lower() == "customer":
            alertas.append({"tipo": "cliente", "texto": "Ya es cliente en HubSpot"})
    else:
        e["hubspot"] = None

    sin_hs = [l["email"] or l["nombre"] for l in e["leads"] if not l.get("hs_id")]
    if sin_hs and (principal or contactos):
        alertas.append({"tipo": "contactos_faltantes",
                        "texto": f"{len(sin_hs)} de {len(e['leads'])} contactos no están en HubSpot"})

    # Actividades: unión de lo asociado a la(s) empresa(s) y a los contactos de la base
    claves = set()
    for c in candidatas:
        for tipo in PROPS_ACTIVIDAD:
            claves.update((tipo, i) for i in asoc[("companies", tipo)].get(c["id"], []))
    for l in contactos:
        for tipo in PROPS_ACTIVIDAD:
            claves.update((tipo, i) for i in asoc[("contacts", tipo)].get(l["hs_id"], []))

    lista, antes = [], []
    for clave in claves:
        p = actividades.get(clave)
        if not p:
            continue
        fecha = _fecha(p.get("hs_timestamp") or p.get("hs_meeting_start_time") or p.get("hs_createdate"))
        if not fecha:
            continue
        (antes if fecha[:10] < inicio else lista).append(_describir(clave[0], clave[1], p, fecha, rep_owner, nombre_owner))
    lista.sort(key=lambda a: a["fecha"])
    e["actividades"] = lista

    # Tratamiento previo a la base: toques salientes, respuestas y reuniones antes del lunes de inicio
    tratos = sorted((a for a in antes if a["saliente"] or a["respuesta"] or a["tipo"] == "reunion"),
                    key=lambda a: a["fecha"])
    e["previo"] = None
    if tratos:
        ultimo_previo = tratos[-1]["fecha"]
        dias_antes = (date.fromisoformat(inicio) - date.fromisoformat(ultimo_previo[:10])).days
        e["previo"] = {
            "primero": tratos[0]["fecha"], "ultimo": ultimo_previo, "dias_antes": dias_antes,
            "reciente": dias_antes <= DIAS_PREVIO_RECIENTE, "total": len(tratos),
            "correos": sum(a["tipo"] == "correo" and a["saliente"] for a in tratos),
            "respuestas": sum(a["respuesta"] for a in tratos),
            "llamadas": sum(a["tipo"] == "llamada" for a in tratos),
            "reuniones": sum(a["tipo"] == "reunion" for a in tratos),
            "mensajes": sum(a["tipo"] == "mensaje" for a in tratos),
            "owners": sorted({a["owner"] for a in tratos if a["owner"]}),
            "ultimos": [{k: a.get(k) for k in ("fecha", "tipo", "owner", "titulo", "detalle", "saliente", "respuesta")}
                        for a in tratos[-5:]][::-1],
        }
        if e["previo"]["reciente"]:
            alertas.append({"tipo": "tratada_antes", "texto":
                f"Ya se trabajaba antes de la base: {len(tratos)} toques, el último hace {dias_antes} días "
                f"({', '.join(e['previo']['owners']) or 'sin owner'})"})

    # Deals
    ids_deal = set()
    for c in candidatas:
        ids_deal.update(asoc[("companies", "deals")].get(c["id"], []))
    for l in contactos:
        ids_deal.update(asoc[("contacts", "deals")].get(l["hs_id"], []))
    e["deals"] = []
    for i in sorted(ids_deal):
        d = deals.get(i)
        if not d:
            continue
        etapa = etapas.get(d.get("dealstage") or "", {})
        cerrado = d.get("hs_is_closed") == "true" or etapa.get("cerrada", False)
        creado = _fecha(d.get("createdate"))
        e["deals"].append({
            "id": i, "nombre": d.get("dealname"), "etapa": etapa.get("etiqueta", d.get("dealstage")),
            "pipeline": etapa.get("pipeline"), "monto": d.get("amount"), "moneda": d.get("deal_currency_code"),
            "creado": creado, "cerrado": cerrado, "ganado": d.get("hs_is_closed_won") == "true",
            "owner": nombre_owner(d.get("hubspot_owner_id")),
            "nuevo": bool(creado and creado[:10] >= inicio),
        })
        if not cerrado and creado and creado[:10] < inicio:
            alertas.append({"tipo": "deal_previo", "texto": f"Deal abierto previo: {d.get('dealname')}"})

    de_otro = [a for a in lista if a["owner_id"] and rep_owner and a["owner_id"] != rep_owner]
    if de_otro:
        quienes = sorted({a["owner"] or "?" for a in de_otro})
        alertas.append({"tipo": "toque_de_otro", "texto": f"{len(de_otro)} actividades de otro owner: {', '.join(quienes)}"})

    # Estado y conteos
    salientes = [a for a in lista if a["saliente"]]
    respuestas = [a for a in lista if a["respuesta"]]
    respuestas_contacto = [l["hs_ultima_respuesta"] for l in contactos
                           if l.get("hs_ultima_respuesta") and l["hs_ultima_respuesta"][:10] >= inicio]
    reuniones = [a for a in lista if a["tipo"] == "reunion"]
    deals_nuevos = [d for d in e["deals"] if d["nuevo"]]

    # Señales de la secuencia en Amplemarket (solo en bases por secuencia)
    am = [l["am"] for l in e["leads"] if l.get("am")]
    am_contacto = min((x["primer_contacto"] for x in am if x["primer_contacto"]), default=None)
    am_respondio = any(x["respondio"] for x in am)
    if am:
        cuenta = lambda *estados: sum(x["estado"] in estados for x in am)
        e["amplemarket"] = {
            "leads": len(am), "en_secuencia": cuenta("in_sequence"), "por_iniciar": cuenta("ready", "draft"),
            "completados": cuenta("completed"), "pausados": cuenta("paused"), "removidos": cuenta("removed"),
            "rebotados": cuenta("hard_bounced", "spam_blocked", "email_unavailable", "error", "unsubscribed"),
            "contactados": sum(bool(x["primer_contacto"]) for x in am), "respondieron": sum(x["respondio"] for x in am),
            "aperturas": sum(x["aperturas"] for x in am), "primer_contacto": am_contacto,
        }
        if am_contacto and not salientes:
            alertas.append({"tipo": "sin_registro_hubspot",
                            "texto": f"Amplemarket la contactó el {am_contacto[:10]} pero HubSpot no tiene esa actividad"})
        if am_respondio and not respuestas and not respuestas_contacto:
            alertas.append({"tipo": "sin_registro_hubspot",
                            "texto": "Respondió en Amplemarket pero la respuesta no está en HubSpot"})
        if e["amplemarket"]["rebotados"] == len(am):
            alertas.append({"tipo": "sin_canal", "texto": "Todos sus contactos rebotaron o no tienen email válido"})

    hitos = {
        "contactada": min([a["fecha"] for a in salientes] + ([am_contacto] if am_contacto else []), default=None),
        "respondio": min([a["fecha"] for a in respuestas] + respuestas_contacto, default=None),
        "reunion": min((a["fecha"] for a in reuniones), default=None),
        "deal": min((d["creado"] for d in deals_nuevos), default=None),
    }
    if deals_nuevos:
        estado = "deal"
    elif reuniones:
        estado = "reunion"
    elif hitos["respondio"] or am_respondio:
        estado = "respondio"
    elif salientes or am_contacto:
        estado = "contactada"
    elif principal or contactos:
        estado = "sin_tocar"
    else:
        estado = "no_en_hubspot"

    ultimo = max((a["fecha"] for a in lista), default=None)
    e.update({
        "estado": estado,
        "hitos": hitos,
        "alertas": alertas,
        "ultimo_toque": ultimo,
        "dias_sin_toque": (date.today() - date.fromisoformat((ultimo or inicio)[:10])).days,
        "conteos": {
            "correos": sum(a["tipo"] == "correo" and a["saliente"] for a in lista),
            "respuestas": sum(a["tipo"] == "correo" and a["respuesta"] for a in lista),
            "llamadas": sum(a["tipo"] == "llamada" for a in lista),
            "conectadas": sum(a["tipo"] == "llamada" and a["respuesta"] for a in lista),
            "mensajes": sum(a["tipo"] == "mensaje" for a in lista),
            "reuniones": len(reuniones),
            "notas": sum(a["tipo"] == "nota" for a in lista),
            "contactos_hs": len(contactos),
            "contactos_base": len(e["leads"]),
        },
    })


def _describir(tipo, id_, p, fecha, rep_owner, nombre_owner) -> dict:
    owner_id = p.get("hubspot_owner_id")
    a = {"id": id_, "objeto": tipo, "fecha": fecha, "owner_id": owner_id, "owner": nombre_owner(owner_id),
         "del_rep": bool(rep_owner and owner_id == rep_owner), "saliente": False, "respuesta": False}
    if tipo == "emails":
        entrante = p.get("hs_email_direction") == "INCOMING_EMAIL"
        asunto = p.get("hs_email_subject") or "(sin asunto)"
        automatico = entrante and bool(NO_ES_RESPUESTA.match(asunto))
        detalle = ("Respuesta automática o de calendario" if automatico else "Respuesta recibida") if entrante \
            else (p.get("hs_email_status") or "Enviado").capitalize()
        a.update(tipo="correo", titulo=asunto, detalle=detalle,
                 saliente=not entrante, respuesta=entrante and not automatico)
    elif tipo == "calls":
        conectada = p.get("hs_call_disposition") == LLAMADA_CONECTADA
        seg = _entero(p.get("hs_call_duration")) // 1000
        a.update(tipo="llamada", titulo=p.get("hs_call_title") or "Llamada",
                 detalle=("Conectada" if conectada else "Sin conectar") + (f" · {seg // 60}m{seg % 60:02d}s" if seg else ""),
                 saliente=p.get("hs_call_direction") != "INBOUND", respuesta=conectada)
    elif tipo == "meetings":
        a.update(tipo="reunion", titulo=p.get("hs_meeting_title") or "Reunión",
                 detalle=(p.get("hs_meeting_outcome") or "Agendada").replace("_", " ").capitalize(),
                 saliente=True)
    elif tipo == "communications":
        canal = (p.get("hs_communication_channel_type") or "mensaje").replace("_", " ")
        a.update(tipo="mensaje", titulo=canal.capitalize(),
                 detalle=_sin_html(p.get("hs_communication_body") or "")[:140], saliente=True)
    else:
        texto = (p.get("hs_note_body") or "")
        a.update(tipo="nota", titulo="Nota", detalle=_sin_html(texto)[:160])
    return a


def _sin_html(texto: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", texto)).strip()


def _resumir(empresas: list[dict]) -> dict:
    n = len(empresas)
    por_estado = {s: sum(e["estado"] == s for e in empresas) for s in ESTADOS}
    alcanzo = lambda s: sum(ESTADOS.index(e["estado"]) >= ESTADOS.index(s) for e in empresas)
    return {
        "empresas": n,
        "contactos": sum(len(e["leads"]) for e in empresas),
        "por_estado": por_estado,
        "en_hubspot": n - por_estado["no_en_hubspot"],
        "contactadas": alcanzo("contactada"),
        "respondieron": alcanzo("respondio"),
        "reuniones": alcanzo("reunion"),
        "deals": alcanzo("deal"),
        "con_alertas": sum(bool(e["alertas"]) for e in empresas),
        "actividades": sum(len(e["actividades"]) for e in empresas),
        "del_rep": sum(a["del_rep"] for e in empresas for a in e["actividades"]),
        "am_por_iniciar": sum(e.get("amplemarket", {}).get("por_iniciar", 0) for e in empresas),
        "am_en_secuencia": sum(e.get("amplemarket", {}).get("en_secuencia", 0) for e in empresas),
        "am_rebotados": sum(e.get("amplemarket", {}).get("rebotados", 0) for e in empresas),
        "sin_registro_hs": sum(any(a["tipo"] == "sin_registro_hubspot" for a in e["alertas"]) for e in empresas),
        "contactos_hs": _resumir_contactos([l for e in empresas for l in e["leads"]]),
    }


def _resumir_contactos(leads: list[dict]) -> dict:
    """Validación por contacto: de los trabajados en Amplemarket, cuántos tienen tratamiento en HubSpot."""
    ts = [l["hs_trat"] for l in leads if l.get("hs_trat")]
    trab = [t for t in ts if t["trabajado"]]
    alcanzo = lambda n: sum(NIVELES_HS.index(t["nivel"]) >= NIVELES_HS.index(n) for t in ts)
    return {
        "total": len(ts), "trabajados": len(trab), "respondieron": sum(t["respondio"] for t in ts),
        "en_hubspot": alcanzo("contacto"), "trabajados_en_hs": sum(t["nivel"] != "no_en_hubspot" for t in trab),
        "con_actividad": sum(bool(t["salientes"]) for t in trab),
        "prospecto": alcanzo("prospecto"), "lead": alcanzo("lead"), "deal": alcanzo("deal"),
        "por_nivel": {n: sum(t["nivel"] == n for t in ts) for n in NIVELES_HS},
        "con_brechas": sum(bool(t["brechas"]) for t in ts),
        "brechas": {k: sum(any(b["tipo"] == k for b in t["brechas"]) for t in ts)
                    for k in ("no_en_hubspot", "sin_actividad", "sin_avance")},
    }


def _leer_json(ruta: Path):
    try:
        return json.loads(ruta.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _guardar(resultado: dict):
    (DATOS / "snapshots").mkdir(parents=True, exist_ok=True)
    texto = json.dumps(resultado, ensure_ascii=False)
    (DATOS / "ultimo.json").write_text(texto)
    sello = resultado["generado"][:16].replace(":", "")
    (DATOS / "snapshots" / f"{sello}.json").write_text(texto)
    with open(DATOS / "historial.jsonl", "a") as f:
        for b in resultado["bases"]:
            if b.get("congelada"):
                continue
            for e in b["empresas"]:
                f.write(json.dumps({"ts": resultado["generado"], "lista_id": b["lista_id"],
                                    "clave": e["clave"], "estado": e["estado"],
                                    "alertas": [a["tipo"] for a in e["alertas"]]}, ensure_ascii=False) + "\n")


def historial(lista_id: str, clave: str) -> list[dict]:
    """Cambios de estado de una empresa entre sincronizaciones (solo cuando cambia)."""
    cambios, previo = [], None
    ruta = DATOS / "historial.jsonl"
    if not ruta.exists():
        return cambios
    with open(ruta) as f:
        for linea in f:
            r = json.loads(linea)
            if r["lista_id"] == lista_id and r["clave"] == clave and r["estado"] != previo:
                cambios.append({"ts": r["ts"], "estado": r["estado"]})
                previo = r["estado"]
    return cambios


if __name__ == "__main__":
    sincronizar()
