"""Vistas ejecutiva y operativa de las gestiones outbound, armadas desde la última sincronización.

Todo número sale de registros de datos/ultimo.json (Amplemarket + HubSpot) y cada fila conserva su
identificador y sus enlaces de origen. Lo que el dato no trae va como None («sin datos») con la forma
de instrumentarlo: aquí no se estima ni se rellena nada.

Uso:
    .venv/bin/python gestiones.py vista [salida.html]   # HTML autónomo con los datos incrustados
    .venv/bin/python gestiones.py resumen               # embudo y alertas por consola

La vista incrusta contactos (PII): es un archivo local, no se publica.
"""
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import entregabilidad as en
import segmentos

RAIZ = Path(__file__).parent
DATOS = RAIZ / "datos"
PLANTILLA = RAIZ / "static" / "gestiones.html"

DIAS_VENCIDA = 7        # hipótesis: un lead sin lanzar a los 7 días del lunes de su base está vencido
HORAS_DESACTUALIZADO = 26

SIN_DATOS = {
    "entregados": "Amplemarket no expone aceptación por mensaje en la API usada. Instrumentar con los "
                  "webhooks de Amplemarket (email sent / bounced) guardando el id del mensaje.",
    "respuesta_positiva": "El dato solo trae «respondió» (sí/no). Instrumentar clasificando la respuesta "
                          "(etiqueta de Amplemarket o propiedad en HubSpot) y sincronizándola.",
    "quejas_spam": "No hay fuente: Amplemarket no la reporta y Google Postmaster Tools no está conectado. "
                   "Dar de alta platzi.com, platzi.com.co y platzi.com.mx en Postmaster Tools.",
    "costos": "No hay costo por contacto, licencia ni crédito en ninguna fuente sincronizada. Instrumentar "
              "con un costo por base (créditos de enriquecimiento + licencias prorrateadas) en equipo.json.",
    "proxima_fecha": "El lead trae el paso actual pero no su fecha; las fechas están en /tasks y hoy no se "
                     "guardan por contacto.",
}

DEFINICIONES = [
    {"metrica": "Inscritos", "numerador": "Leads dentro de una secuencia seguida (una fila por secuencia y correo)",
     "denominador": "—", "ventana": "Estado al momento de la sincronización"},
    {"metrica": "Elegibles", "numerador": "Inscritos con correo y sin estado «sin email válido»",
     "denominador": "Inscritos", "ventana": "Estado actual"},
    {"metrica": "Contactados", "numerador": "Inscritos con fecha de primer contacto en Amplemarket",
     "denominador": "Inscritos", "ventana": "Desde el alta en la secuencia",
     "limite": "El primer contacto puede ser un paso que no es correo (LinkedIn, llamada)."},
    {"metrica": "Rebote duro", "numerador": "Contactos cuyo estado actual es hard_bounced",
     "denominador": "Contactados", "ventana": "Estado actual",
     "limite": "Es por contacto, no por mensaje: no hay conteo de envíos ni código SMTP."},
    {"metrica": "Bloqueo por política", "numerador": "Contactos en spam_blocked", "denominador": "Contactados",
     "ventana": "Estado actual", "limite": "Rechazo del servidor receptor; no es una queja del destinatario."},
    {"metrica": "Con apertura", "numerador": "Contactados con ≥1 apertura reportada", "denominador": "Contactados",
     "ventana": "Acumulado", "limite": "Píxel de seguimiento: lo inflan los prefetch de Apple/Google y los "
                                        "filtros de seguridad, y lo ocultan los clientes que bloquean imágenes. Señal secundaria."},
    {"metrica": "Respondieron", "numerador": "Contactos con respuesta en Amplemarket o con correo entrante en "
                                             "HubSpot que no sea automático (ausencias y calendario van aparte)",
     "denominador": "Contactados",
     "ventana": "Acumulado desde el primer contacto"},
    {"metrica": "Cita agendada", "numerador": "Contactos con «scheduled a call» en Amplemarket",
     "denominador": "Contactados", "ventana": "Acumulado"},
    {"metrica": "Reunión registrada / realizada", "numerador": "Reuniones de HubSpot asociadas a la cuenta o a "
                                                               "sus contactos; realizada = resultado Completed",
     "denominador": "Cuentas contactadas", "ventana": "Desde el lunes de la base",
     "limite": "Por cuenta, no por contacto; atribución por fecha, no por origen."},
    {"metrica": "Deal nuevo / ganado", "numerador": "Deals de HubSpot creados desde el lunes de la base",
     "denominador": "Cuentas contactadas", "ventana": "Desde el lunes de la base",
     "limite": "Atribución por coincidencia de cuenta y fecha: un deal de inbound en la misma cuenta cuenta igual."},
]


def _comercial(am: dict, t: dict) -> str:
    if any(d.get("nuevo") for d in t.get("deals", [])):
        return "deal"
    if am.get("llamada_agendada"):
        return "cita"
    if am.get("respondio") or t.get("respuestas"):
        return "respondio"
    return "contactado" if am.get("primer_contacto") else "sin_contactar"


def _fila(b: dict, e: dict, l: dict, portal, mx: dict, hoy: date) -> dict:
    am, t = l["am"], l.get("hs_trat") or {}
    email = en.normalizar_email(l.get("email"))
    tecnico = en.estado_tecnico(am)
    comercial = _comercial(am, t)
    fallo = en.clasificar_fallo(am)
    brechas = [x["tipo"] for x in t.get("brechas", [])]
    dias_base = (hoy - date.fromisoformat(b["inicio"])).days

    cola, accion = None, None
    if fallo:
        cola = "bloqueada" if fallo["clase"] in ("politica",) or tecnico == "baja" else "fallida"
        accion = fallo["accion"]
    elif tecnico == "sin_enviar":
        cola = "vencida" if dias_base >= DIAS_VENCIDA else "pendiente"
        accion = "Lanzar la secuencia para este lead o retirarlo de la base."
    elif "sin_avance" in brechas:
        cola, accion = "por_gestionar", "Respondió: crear el Lead o el Deal en HubSpot y registrar el siguiente paso."
    elif "no_en_hubspot" in brechas or "sin_actividad" in brechas:
        cola, accion = "sin_registro", "Amplemarket lo contactó y HubSpot no lo refleja: revisar la sincronización del buzón."
    elif tecnico == "en_curso":
        accion = f"Siguiente paso de la secuencia: {am.get('paso') or 'sin datos'}"
    elif tecnico == "pausa_ausencia":
        accion = "Respuesta automática de ausencia: Amplemarket retoma solo."

    dominio = email.split("@")[1] if "@" in email else None
    hs = e.get("hubspot") or {}
    return {
        "id": f"{am.get('secuencia_id')}:{email or en.normalizar_email(l.get('nombre'))}",
        "email": email, "nombre": l.get("nombre"), "cargo": l.get("cargo"), "linkedin": l.get("linkedin"),
        "empresa": e.get("nombre"), "cuenta": f"{b['lista_id']}|{e['clave']}",
        "responsable": b.get("rep_nombre"), "rep_email": b.get("rep_email"), "segmento": b.get("segmento"),
        "semana": b.get("semana_clave"), "inicio_base": b.get("inicio"),
        "secuencia": am.get("secuencia"), "secuencia_id": am.get("secuencia_id"), "paso": am.get("paso"),
        "tecnico": tecnico, "comercial": comercial, "cita": bool(am.get("llamada_agendada")), "hs_nivel": t.get("nivel"),
        "primer_contacto": am.get("primer_contacto"), "ultimo_toque": t.get("ultima") or am.get("primer_contacto"),
        "aperturas": am.get("aperturas") or 0, "buzon": am.get("buzon"),
        "verificacion": en.verificacion(l.get("validacion")),
        "proveedor": en.proveedor((mx.get(dominio) or {}).get("mx")) if dominio in mx else None,
        "cola": cola, "accion": accion, "motivo": (fallo or {}).get("detalle") or am.get("motivo"),
        "clase_fallo": (fallo or {}).get("clase"), "brechas": brechas,
        "url_secuencia": f"https://app.amplemarket.com/dashboard/sequences/{am['secuencia_id']}"
                         if am.get("secuencia_id") else None,
        "url_contacto": f"https://app.hubspot.com/contacts/{portal}/record/0-1/{l['hs_id']}"
                        if portal and l.get("hs_id") else None,
        "url_empresa": f"https://app.hubspot.com/contacts/{portal}/record/0-2/{hs['id']}"
                       if portal and hs.get("id") else None,
    }


def _paso(n, etiqueta, nota=None, falta=None) -> dict:
    return {"n": n, "etiqueta": etiqueta} | ({"nota": nota} if nota else {}) | \
        ({"como_instrumentar": SIN_DATOS[falta]} if falta else {})


def _embudo(filas: list[dict], datos: dict) -> dict:
    cuenta = lambda f: sum(1 for x in filas if f(x))
    empresas = [e for b in datos.get("bases", []) for e in b.get("empresas", [])]
    orden = ["no_en_hubspot", "sin_tocar", "contactada", "respondio", "reunion", "deal"]
    alcanzo = lambda s: sum(orden.index(e.get("estado", "sin_tocar")) >= orden.index(s) for e in empresas)
    reuniones = {a["id"]: a for e in empresas for a in e.get("actividades", []) if a.get("tipo") == "reunion"}
    deals = {d["id"]: d for e in empresas for d in e.get("deals", []) if d.get("nuevo")}
    return {
        "contactos": {
            "inscritos": _paso(len(filas), "Inscritos"),
            "elegibles": _paso(cuenta(lambda x: x["email"] and x["tecnico"] != "sin_email_valido"), "Elegibles"),
            "contactados": _paso(cuenta(lambda x: x["primer_contacto"]), "Contactados"),
            "entregados": _paso(None, "Entregados (aceptados)", falta="entregados"),
            "con_apertura": _paso(cuenta(lambda x: x["primer_contacto"] and x["aperturas"] > 0), "Con apertura",
                                  nota="señal secundaria, poco fiable"),
            "respondieron": _paso(cuenta(lambda x: x["comercial"] in ("respondio", "cita", "deal")
                                         and x["primer_contacto"]), "Respondieron"),
            "respuesta_positiva": _paso(None, "Respuesta positiva", falta="respuesta_positiva"),
            "cita_agendada": _paso(cuenta(lambda x: x["cita"]), "Cita agendada"),
        },
        "cuentas": {
            "cuentas": _paso(len(empresas), "Cuentas en bases"),
            "contactadas": _paso(alcanzo("contactada"), "Contactadas"),
            "respondieron": _paso(alcanzo("respondio"), "Respondieron"),
            "reunion": _paso(sum(any(a.get("tipo") == "reunion" for a in e.get("actividades", []))
                                 for e in empresas), "Con reunión registrada"),
            "reunion_realizada": _paso(sum(a.get("detalle") == "Completed" for a in reuniones.values()),
                                       "Reuniones realizadas", nota="resultado Completed en HubSpot"),
            "deal": _paso(len(deals), "Deals nuevos"),
            "ganado": _paso(sum(bool(d.get("ganado")) for d in deals.values()), "Ganados"),
        },
        "pipeline": {moneda or "sin moneda": round(sum(float(d.get("monto") or 0) for d in deals.values()
                                                       if d.get("moneda") == moneda), 2)
                     for moneda in sorted({d.get("moneda") for d in deals.values()}, key=str)},
        "ingresos_ganados": {moneda or "sin moneda": round(sum(float(d.get("monto") or 0) for d in deals.values()
                                                               if d.get("moneda") == moneda and d.get("ganado")), 2)
                             for moneda in sorted({d.get("moneda") for d in deals.values() if d.get("ganado")}, key=str)},
    }


def _corte(filas: list[dict], leads: dict, clave: str) -> list[dict]:
    grupos: dict[str, list] = {}
    for f in filas:
        grupos.setdefault(str(f.get(clave) if f.get(clave) is not None else "sin datos"), []).append(f)
    res = []
    for nombre, g in grupos.items():
        s = en.salud([leads[f["id"]] for f in g])
        res.append({"nombre": nombre, "inscritos": s["inscritos"], "contactados": s["contactados"],
                    "rebote_duro": s["estados"].get("rebote_duro", 0),
                    "bloqueo_politica": s["estados"].get("bloqueo_politica", 0),
                    "sin_email_valido": s["estados"].get("sin_email_valido", 0),
                    "sin_enviar": s["estados"].get("sin_enviar", 0),
                    "respondieron": s["respondieron"], "con_apertura": s["con_apertura"],
                    "cita_agendada": s["cita_agendada"], "sin_verificar": s["contactados_sin_verificar"],
                    "tasa_rebote_duro": s["tasa_rebote_duro"], "tasa_bloqueo_politica": s["tasa_bloqueo_politica"],
                    "tasa_respuesta": s["tasa_respuesta"], "nivel": s["nivel"], "alertas": s["alertas"]})
    return sorted(res, key=lambda r: -r["inscritos"])


def _semana_contacto(f: dict) -> str | None:
    if not f["primer_contacto"]:
        return None
    anio, semana, _ = date.fromisoformat(f["primer_contacto"][:10]).isocalendar()
    return f"{anio}-S{semana:02d}"


def tendencia(fotos: list[dict]) -> list[dict]:
    """Cierre de cada día (la última foto del día): acumulados de contactados, rebotes y respuestas."""
    por_dia = {}
    for d in sorted(fotos, key=lambda d: d.get("generado") or ""):
        if d.get("generado"):
            por_dia[d["generado"][:10]] = d
    serie = []
    for dia, d in sorted(por_dia.items()):
        s = en.salud([l for _, _, l in en.inscripciones(d)])
        if s["inscritos"]:
            serie.append({"dia": dia, "inscritos": s["inscritos"], "contactados": s["contactados"],
                          "rebote_duro": s["estados"].get("rebote_duro", 0),
                          "bloqueo_politica": s["estados"].get("bloqueo_politica", 0),
                          "respondieron": s["respondieron"], "tasa_rebote_duro": s["tasa_rebote_duro"]})
    return serie


def _integraciones(datos: dict, ahora: datetime) -> list[dict]:
    generado = datos.get("generado")
    horas = (ahora - datetime.fromisoformat(generado)).total_seconds() / 3600 if generado else None
    estado = "sin_datos" if horas is None else "desactualizado" if horas > HORAS_DESACTUALIZADO else "ok"
    hay_leads = any(True for _ in en.inscripciones(datos))
    return [
        {"nombre": "Amplemarket · API pública", "que": "Usuarios, secuencias y tareas",
         "estado": estado if datos.get("secuencias") else "sin_datos", "ultima": generado},
        {"nombre": "Amplemarket · sesión web (Chrome :9361)", "que": "Leads de cada secuencia y su estado",
         "estado": estado if hay_leads else "sin_datos", "ultima": generado,
         "nota": "Depende de una sesión iniciada a mano; si expira, la sincronización falla completa."},
        {"nombre": "HubSpot", "que": "Empresas, contactos, actividades, reuniones y deals",
         "estado": estado if datos.get("portal_id") else "sin_datos", "ultima": generado},
        {"nombre": "Google Postmaster Tools", "que": "Tasa de spam y reputación del dominio",
         "estado": "no_conectado", "ultima": None},
        {"nombre": "Webhooks de Amplemarket", "que": "Eventos por mensaje (enviado, rebote, respuesta)",
         "estado": "no_conectado", "ultima": None},
    ]


def construir(datos: dict, fotos: list[dict] | None = None, mx: dict | None = None,
              ahora: datetime | None = None) -> dict:
    ahora = ahora or datetime.now(timezone.utc)
    hoy, mx = ahora.date(), mx or {}
    portal = datos.get("portal_id")
    filas, leads = [], {}
    for b, e, l in en.inscripciones(datos):
        f = _fila(b, e, l, portal, mx, hoy)
        filas.append(f)
        leads[f["id"]] = l
    for f in filas:
        f["semana_contacto"] = _semana_contacto(f)
        f["dominio_buzon"] = f["buzon"].split("@")[-1] if isinstance(f["buzon"], str) and "@" in f["buzon"] else None

    total = en.salud(list(leads.values()))
    cortes = {k: _corte(filas, leads, k) for k in
              ("secuencia", "responsable", "segmento", "semana", "semana_contacto", "dominio_buzon", "proveedor",
               "verificacion")}
    alertas = [{"ambito": "Total", "nombre": "Todas las secuencias"} | a for a in total["alertas"]]
    for ambito, clave in (("Secuencia", "secuencia"), ("Responsable", "responsable"), ("Proveedor receptor", "proveedor")):
        for c in cortes[clave]:
            alertas += [{"ambito": ambito, "nombre": c["nombre"]} | a for a in c["alertas"]]
    if total["contactados"] and total["contactados_sin_verificar"] / total["contactados"] > 0.2:
        alertas.insert(0, {"ambito": "Total", "nombre": "Verificación", "nivel": "aviso", "metrica": "sin_verificar",
                           "valor": round(total["contactados_sin_verificar"] / total["contactados"], 4), "umbral": 0.2,
                           "texto": f"{total['contactados_sin_verificar']} de {total['contactados']} contactados "
                                    "salieron sin verificación vigente del correo"})
    orden = {"pausar": 0, "aviso": 1}
    alertas.sort(key=lambda a: (orden.get(a["nivel"], 2), -(a.get("valor") or 0)))

    cuentas = {}
    for b in datos.get("bases", []):
        for e in b.get("empresas", []):
            cuentas[f"{b['lista_id']}|{e['clave']}"] = {
                "estado": e.get("estado"), "alertas": [a["texto"] for a in e.get("alertas", [])],
                "actividades": [{k: a.get(k) for k in ("fecha", "tipo", "titulo", "detalle", "owner", "saliente", "respuesta")}
                                for a in e.get("actividades", [])][-40:],
                "deals": [{k: d.get(k) for k in ("nombre", "etapa", "monto", "moneda", "nuevo", "ganado")}
                          for d in e.get("deals", [])],
            }
    colas: dict[str, int] = {}
    for f in filas:
        if f["cola"]:
            colas[f["cola"]] = colas.get(f["cola"], 0) + 1
    fechas = sorted(f["primer_contacto"][:10] for f in filas if f["primer_contacto"])
    return {
        "generado": datos.get("generado"), "armado": ahora.isoformat(timespec="seconds"),
        "ventana": {"desde": fechas[0] if fechas else None, "hasta": fechas[-1] if fechas else None,
                    "dias": (date.fromisoformat(fechas[-1]) - date.fromisoformat(fechas[0])).days + 1 if fechas else 0},
        "integraciones": _integraciones(datos, ahora), "definiciones": DEFINICIONES,
        "umbrales": {"valores": en.UMBRALES, "muestra_minima": en.MUESTRA_MINIMA,
                     "nota": "Hipótesis operativas ajustables en entregabilidad.py; no son requisitos de los proveedores."},
        "embudo": _embudo(filas, datos), "salud": total, "alertas": alertas, "cortes": cortes,
        "tendencia": tendencia(fotos) if fotos else [],
        "bajas": {"valor": total["estados"].get("baja", 0),
                  "nota": "0 puede significar que nadie se dio de baja o que las secuencias no llevan enlace de baja: validar."},
        "quejas_spam": {"valor": None, "como_instrumentar": SIN_DATOS["quejas_spam"]},
        "costos": {"valor": None, "como_instrumentar": SIN_DATOS["costos"]},
        "sin_datos": SIN_DATOS, "colas": colas, "duplicados": len(en.duplicados(datos)),
        "filas": filas, "cuentas": cuentas,
    }


def cargar(con_mx: bool = True) -> dict:
    datos = segmentos.aplicar(json.loads((DATOS / "ultimo.json").read_text()))
    fotos = [json.loads(r.read_text()) for r in sorted((DATOS / "snapshots").glob("*.json"))]
    mx = {}
    if con_mx:
        dominios = {l["email"].split("@")[1] for _, _, l in en.inscripciones(datos) if en.sintaxis_ok(l.get("email") or "")}
        mx = en.CacheMX().resolver(dominios)
    return construir(datos, fotos, mx)


def escribir_vista(salida: Path) -> Path:
    g = cargar()
    incrustado = json.dumps(g, ensure_ascii=False).replace("</", "<\\/")
    salida.write_text(PLANTILLA.read_text().replace("/*GESTIONES*/null", incrustado, 1))
    return salida


def main(args: list[str]) -> None:
    if args[:1] == ["vista"]:
        defecto = Path.home() / "Downloads" / f"Follow up outbound · gestiones {date.today().isoformat()}.html"
        print(escribir_vista(Path(args[1]) if len(args) > 1 else defecto))
    elif args == ["resumen"]:
        g = cargar(con_mx=False)
        for grupo in ("contactos", "cuentas"):
            print(grupo.upper())
            for p in g["embudo"][grupo].values():
                print(f"  {p['etiqueta']:28} {'sin datos' if p['n'] is None else p['n']}")
        print("PIPELINE", g["embudo"]["pipeline"], "· COLAS", g["colas"])
        for a in g["alertas"]:
            print(f"  [{a['nivel']}] {a['ambito']} · {a['nombre']}: {a['texto']}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
