"""Higiene de correos y salud de envío de las secuencias de Amplemarket.

Quien envía es Amplemarket, desde los buzones de Google Workspace de cada seller: esta app no envía
ni reintenta nada. Lo que sí puede hacer, y hace este módulo, es:

1. Revisar una base ANTES de importarla a una secuencia (sintaxis, dominio, MX, supresión, duplicados).
2. Mantener la lista global de supresión a partir de lo que Amplemarket ya reportó.
3. Separar el estado técnico del envío del estado comercial y clasificar cada fallo.
4. Medir la salud de envío con numeradores y denominadores explícitos, y avisar cuando se deteriora.

Uso:
    .venv/bin/python entregabilidad.py verificar base.csv   # escribe «base · verificada.csv» al lado
    .venv/bin/python entregabilidad.py supresion            # reconstruye datos/supresion.json
    .venv/bin/python entregabilidad.py salud                # salud por secuencia del último dato

Límite que hay que tener presente: sintaxis + dominio + MX dicen que el dominio puede recibir correo,
NO que el buzón exista. Un «apto» aquí sigue necesitando la verificación del proveedor.
"""
import csv
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).parent
DATOS = RAIZ / "datos"
RUTA_SUPRESION = DATOS / "supresion.json"
RUTA_CACHE_MX = DATOS / "cache_mx.json"

# ── Política (hipótesis operativas ajustables; NO son requisitos de ningún proveedor) ──────────────
REVALIDAR_DIAS = 30          # una verificación del proveedor más vieja que esto vuelve a «desconocido»
CACHE_MX_DIAS = 30
MUESTRA_MINIMA = 50          # contactados mínimos para que una tasa dispare una alerta
EVENTOS_MINIMOS = 3          # y al menos estos casos: uno o dos rebotes no son una tendencia
UMBRALES = {                 # tasa sobre contactos contactados → (aviso, pausa)
    "rebote_duro": (0.02, 0.05),
    "bloqueo_politica": (0.01, 0.03),
}

RE_EMAIL = re.compile(r"^[a-z0-9._%+\-]+@(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
DOMINIOS_PERSONALES = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.es", "live.com",
    "icloud.com", "hotmail.es", "outlook.es", "msn.com", "protonmail.com", "aol.com",
}
BUZONES_DE_ROL = {"info", "contacto", "contact", "ventas", "sales", "admin", "administracion", "rrhh", "hr",
                  "soporte", "support", "hola", "hello", "gerencia", "noreply", "no-reply", "facturacion",
                  "recepcion", "comunicaciones", "marketing", "atencion", "servicioalcliente"}

# Resultado de verificación del proveedor (Amplemarket u otro) → categoría propia
VERIFICACION = {
    "valido": {"verified", "valid", "deliverable", "safe", "ok"},
    "invalido": {"invalid", "undeliverable", "bounced", "hard_bounced", "bad", "email_unavailable"},
    "catch_all": {"catch_all", "catch-all", "catchall", "accept_all", "accept-all", "risky"},
}

# Estado del lead en la secuencia (Amplemarket) → estado técnico del envío
ESTADO_TECNICO = {
    "draft": "sin_enviar", "ready": "sin_enviar", "in_sequence": "en_curso", "completed": "terminado",
    "removed": "retirado", "paused": "pausado", "ooo_waiting": "pausa_ausencia",
    "hard_bounced": "rebote_duro", "spam_blocked": "bloqueo_politica",
    "email_unavailable": "sin_email_valido", "error": "error", "unsubscribed": "baja",
}
ESTADO_TECNICO_TXT = {
    "sin_enviar": "Sin enviar", "en_curso": "En curso", "terminado": "Terminó la secuencia",
    "retirado": "Retirado", "pausado": "Pausado", "pausa_ausencia": "En pausa por ausencia",
    "rebote_duro": "Rebote duro", "bloqueo_politica": "Bloqueado por política",
    "sin_email_valido": "Sin email válido", "error": "Error", "fallo_temporal": "Fallo temporal", "baja": "Baja",
}
# clase del fallo → si se puede reintentar y qué hacer
FALLOS = {
    "rebote_duro": ("permanente", "No reintentar. Suprimir el correo y buscar otro contacto o canal en la cuenta."),
    "sin_email_valido": ("permanente", "No reintentar. Conseguir un correo verificado o seguir por LinkedIn/teléfono."),
    "baja": ("permanente", "No volver a escribir. Queda en la supresión global."),
    "bloqueo_politica": ("politica", "No reenviar el mismo mensaje. Revisar contenido, enlaces y reputación del "
                                     "buzón; seguir por otro canal."),
    "fallo_temporal": ("temporal", "Reintento limitado con espera progresiva (lo administra Amplemarket); si "
                                   "persiste tras 3 intentos, pasar a otro canal."),
    "error": ("desconocido", "Abrir el lead en Amplemarket y leer el motivo; el dato sincronizado no lo trae."),
}
TEMPORAL = re.compile(r"mailbox is full|retry later|temporar|try again|rate limit|timeout|4\.\d\.\d", re.IGNORECASE)
# Estados que entran a la supresión global (bloqueo_politica no: la dirección puede ser válida)
SUPRIMIR = {"rebote_duro": "hard bounce", "sin_email_valido": "sin email válido", "baja": "baja"}


def normalizar_email(email: str | None) -> str:
    return (email or "").strip().lower()


def sintaxis_ok(email: str) -> bool:
    return bool(RE_EMAIL.match(email)) and ".." not in email and len(email) <= 254


def estado_tecnico(am: dict) -> str:
    """Estado técnico del envío de una inscripción. Un `error` con motivo transitorio es fallo temporal."""
    estado = ESTADO_TECNICO.get(am.get("estado") or "draft", "error")
    if estado == "error" and TEMPORAL.search(am.get("motivo") or ""):
        return "fallo_temporal"
    return estado


def clasificar_fallo(am: dict) -> dict | None:
    """{"clase", "reintentar", "accion", "detalle"} si el envío falló; None si no hay fallo."""
    estado = estado_tecnico(am)
    if estado not in FALLOS:
        return None
    clase, accion = FALLOS[estado]
    return {"estado": estado, "clase": clase, "reintentar": clase == "temporal", "accion": accion,
            "detalle": am.get("motivo") or "Amplemarket no entrega código ni motivo del proveedor en este dato"}


def verificacion(resultado: str | None, verificado_en: str | None = None, hoy: date | None = None) -> str:
    """valido | invalido | catch_all | desconocido. Una verificación vencida vuelve a desconocido."""
    r = (resultado or "").strip().lower()
    cat = next((k for k, valores in VERIFICACION.items() if r in valores), "desconocido")
    if cat == "valido" and verificado_en:
        try:
            if ((hoy or date.today()) - date.fromisoformat(verificado_en[:10])).days > REVALIDAR_DIAS:
                return "desconocido"
        except ValueError:
            pass
    return cat


# ── Dominio y MX ────────────────────────────────────────────────────────────────────────────────────
PROVEEDORES = [("google", ("google.com", "googlemail.com")), ("microsoft", ("outlook.com", "office365.us")),
               ("proofpoint", ("pphosted.com", "ppe-hosted.com")), ("mimecast", ("mimecast.com",)),
               ("barracuda", ("barracudanetworks.com",)), ("cisco", ("iphmx.com",)),
               ("zoho", ("zoho.com", "zoho.eu"))]


def proveedor(mx: list[str] | None) -> str:
    """Proveedor receptor a partir de los hosts MX (lo que hay delante puede ser solo un filtro)."""
    if mx is None:
        return "sin_consultar"
    if not mx:
        return "sin_mx"
    for nombre, sufijos in PROVEEDORES:
        if any(h.endswith(s) for h in mx for s in sufijos):
            return nombre
    return "otro"


def consultar_mx(dominio: str) -> dict:
    """{"estado": con_mx | sin_mx | no_existe | sin_consultar, "mx": [...]} con `dig`.
    sin_mx incluye el «null MX» (RFC 7505). No abre ninguna conexión SMTP."""
    try:
        r = subprocess.run(["dig", "+time=3", "+tries=2", "+noall", "+comments", "+answer", "MX", dominio],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {"estado": "sin_consultar", "mx": None}
    if "status: NXDOMAIN" in r.stdout:
        return {"estado": "no_existe", "mx": []}
    if "status: NOERROR" not in r.stdout:
        return {"estado": "sin_consultar", "mx": None}
    mx = []
    for linea in r.stdout.splitlines():
        partes = linea.split()
        if len(partes) >= 6 and partes[3] == "MX":
            host = partes[5].rstrip(".").lower()
            if host:
                mx.append(host)
    return {"estado": "con_mx" if mx else "sin_mx", "mx": sorted(mx)}


class CacheMX:
    """Resultados de MX por dominio en datos/cache_mx.json, válidos CACHE_MX_DIAS."""

    def __init__(self, ruta: Path = RUTA_CACHE_MX, consultar=consultar_mx):
        self.ruta, self.consultar = ruta, consultar
        try:
            self.datos = json.loads(ruta.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.datos = {}

    def _vigente(self, dominio: str) -> bool:
        r = self.datos.get(dominio)
        if not r or r["estado"] == "sin_consultar":
            return False
        return (date.today() - date.fromisoformat(r["ts"])).days <= CACHE_MX_DIAS

    def resolver(self, dominios) -> dict[str, dict]:
        faltan = sorted({d for d in dominios if d and not self._vigente(d)})
        if faltan:
            with ThreadPoolExecutor(max_workers=16) as pool:
                for d, r in zip(faltan, pool.map(self.consultar, faltan)):
                    self.datos[d] = r | {"ts": date.today().isoformat()}
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            self.ruta.write_text(json.dumps(self.datos, ensure_ascii=False))
        return {d: self.datos[d] for d in dominios if d in self.datos}


# ── Revisión previa a la inscripción ────────────────────────────────────────────────────────────────
def revisar_email(email: str, resultado_proveedor: str | None = None, verificado_en: str | None = None,
                  mx: dict | None = None, supresion: dict | None = None, vistos: set | None = None) -> dict:
    """Decide si un correo puede entrar a una secuencia: apto | revisar | bloquear, con su motivo.
    `mx` es el resultado de consultar_mx para su dominio; `vistos`, los correos ya aceptados."""
    email = normalizar_email(email)
    r = {"email": email, "decision": "bloquear", "categoria": "invalido", "motivo": "", "proveedor": None}
    if not email:
        return r | {"motivo": "sin correo"}
    if not sintaxis_ok(email):
        return r | {"motivo": "sintaxis inválida"}
    if supresion and email in supresion:
        return r | {"categoria": "suprimido", "motivo": f"en supresión global: {supresion[email]['motivo']}"}
    if vistos is not None and email in vistos:
        return r | {"categoria": "duplicado", "motivo": "duplicado en la misma base"}
    local, dominio = email.split("@")
    r["proveedor"] = proveedor((mx or {}).get("mx")) if mx else "sin_consultar"
    if mx and mx["estado"] == "no_existe":
        return r | {"motivo": "el dominio no existe"}
    if mx and mx["estado"] == "sin_mx":
        return r | {"motivo": "el dominio no recibe correo (sin MX)"}
    cat = verificacion(resultado_proveedor, verificado_en)
    if cat == "invalido":
        return r | {"motivo": "el proveedor lo marcó inválido"}
    if vistos is not None:
        vistos.add(email)
    r["categoria"] = cat
    if cat == "catch_all":
        return r | {"decision": "revisar", "motivo": "dominio catch-all: no se puede confirmar el buzón"}
    if cat == "desconocido":
        return r | {"decision": "revisar", "motivo": "sin verificación vigente del proveedor"}
    if not mx or mx["estado"] == "sin_consultar":
        return r | {"decision": "revisar", "motivo": "no se pudo consultar el MX"}
    if dominio in DOMINIOS_PERSONALES:
        return r | {"decision": "revisar", "motivo": "correo personal, no corporativo"}
    if local in BUZONES_DE_ROL:
        return r | {"decision": "revisar", "motivo": "buzón de rol, no de una persona"}
    return r | {"decision": "apto", "motivo": "verificado por el proveedor y con MX"}


def revisar_base(filas: list[dict], supresion: dict | None = None, cache: CacheMX | None = None) -> list[dict]:
    """Revisa las filas de una base (dicts con `email` y, si existen, `email_status`/`estado` y
    `verificado_en`). Devuelve una revisión por fila, en el mismo orden."""
    emails = [normalizar_email(f.get("email") or f.get("Email") or f.get("correo")) for f in filas]
    dominios = {e.split("@")[1] for e in emails if sintaxis_ok(e)}
    mx = (cache or CacheMX()).resolver(dominios)
    vistos: set[str] = set()
    return [revisar_email(e, f.get("email_status") or f.get("estado") or f.get("validacion"),
                          f.get("verificado_en"), mx.get(e.split("@")[1]) if "@" in e else None,
                          supresion, vistos)
            for e, f in zip(emails, filas)]


# ── Supresión global ────────────────────────────────────────────────────────────────────────────────
def inscripciones(datos: dict):
    """(base, empresa, lead) de cada lead con datos de secuencia."""
    for b in datos.get("bases", []):
        for e in b.get("empresas", []):
            for l in e.get("leads", []):
                if l.get("am"):
                    yield b, e, l


def construir_supresion(fotos: list[dict], previa: dict | None = None) -> dict:
    """{email: {motivo, estado, desde, secuencia_id, rep}} con hard bounces, sin email válido y bajas
    vistos en cualquier foto. Nunca se quita a nadie: un correo que rebotó sigue suprimido aunque luego
    salga de la secuencia. Las quejas de spam no llegan en el dato de Amplemarket."""
    sup = dict(previa or {})
    for datos in sorted(fotos, key=lambda d: d.get("generado") or ""):
        for b, _, l in inscripciones(datos):
            email, estado = normalizar_email(l.get("email")), estado_tecnico(l["am"])
            if email and estado in SUPRIMIR and email not in sup:
                sup[email] = {"motivo": SUPRIMIR[estado], "estado": estado, "desde": (datos.get("generado") or "")[:10],
                              "secuencia_id": l["am"].get("secuencia_id"), "rep": b.get("rep_email")}
    return sup


def cargar_supresion() -> dict:
    try:
        return json.loads(RUTA_SUPRESION.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def duplicados(datos: dict) -> list[dict]:
    """Correos inscritos más de una vez (otra secuencia, otro rep u otra semana)."""
    por_email: dict[str, list] = {}
    for b, _, l in inscripciones(datos):
        if l.get("email"):
            por_email.setdefault(normalizar_email(l["email"]), []).append(
                {"secuencia": l["am"].get("secuencia"), "rep": b.get("rep_email"), "semana": b.get("semana_clave")})
    return [{"email": e, "inscripciones": v} for e, v in sorted(por_email.items()) if len(v) > 1]


# ── Salud de envío ──────────────────────────────────────────────────────────────────────────────────
def salud(leads: list[dict]) -> dict:
    """Salud de un grupo de inscripciones (leads con `am`). Unidad: contacto inscrito, no mensaje.

    inscritos    = leads en la secuencia
    contactados  = leads con fecha de primer contacto en Amplemarket (cualquier canal del paso 1)
    tasas        = estado técnico actual / contactados. El lead guarda un solo estado: un rebote en el
                   paso 3 y uno en el paso 1 cuentan igual, y no hay conteo por mensaje.
    """
    n = len(leads)
    estados: dict[str, int] = {}
    for l in leads:
        t = estado_tecnico(l["am"])
        estados[t] = estados.get(t, 0) + 1
    contactados = sum(bool(l["am"].get("primer_contacto")) for l in leads)
    sin_verificar = sum(verificacion(l.get("validacion")) != "valido" for l in leads
                        if l["am"].get("primer_contacto"))
    tasa = lambda k: round(estados.get(k, 0) / contactados, 4) if contactados else None
    r = {
        "inscritos": n, "contactados": contactados, "estados": estados,
        "respondieron": sum(bool(l["am"].get("respondio")) for l in leads),
        "con_apertura": sum((l["am"].get("aperturas") or 0) > 0 for l in leads if l["am"].get("primer_contacto")),
        "cita_agendada": sum(bool(l["am"].get("llamada_agendada")) for l in leads),
        "contactados_sin_verificar": sin_verificar,
        "tasa_rebote_duro": tasa("rebote_duro"), "tasa_bloqueo_politica": tasa("bloqueo_politica"),
        "alertas": [],
    }
    r["tasa_respuesta"] = round(r["respondieron"] / contactados, 4) if contactados else None
    for clave, (aviso, pausa) in UMBRALES.items():
        t = tasa(clave)
        if t is None or contactados < MUESTRA_MINIMA or t < aviso or estados.get(clave, 0) < EVENTOS_MINIMOS:
            continue
        nivel = "pausar" if t >= pausa else "aviso"
        r["alertas"].append({
            "nivel": nivel, "metrica": clave, "valor": t, "umbral": pausa if nivel == "pausar" else aviso,
            "texto": f"{ESTADO_TECNICO_TXT[clave]}: {estados.get(clave, 0)} de {contactados} contactados "
                     f"({t:.1%}); umbral de {'pausa' if nivel == 'pausar' else 'aviso'} {(pausa if nivel == 'pausar' else aviso):.0%}"})
    r["nivel"] = "pausar" if any(a["nivel"] == "pausar" for a in r["alertas"]) else "aviso" if r["alertas"] else \
        "ok" if contactados >= MUESTRA_MINIMA else "sin_muestra"
    return r


def salud_por(datos: dict, clave) -> dict[str, dict]:
    grupos: dict[str, list] = {}
    for b, e, l in inscripciones(datos):
        grupos.setdefault(str(clave(b, e, l)), []).append(l)
    return {k: salud(v) for k, v in sorted(grupos.items(), key=lambda kv: -len(kv[1]))}


# ── Consola ─────────────────────────────────────────────────────────────────────────────────────────
def _fotos() -> list[dict]:
    rutas = sorted((DATOS / "snapshots").glob("*.json")) + [DATOS / "ultimo.json"]
    return [json.loads(r.read_text()) for r in rutas if r.exists()]


def _cmd_verificar(ruta: Path) -> None:
    with open(ruta, newline="", encoding="utf-8-sig") as f:
        filas = list(csv.DictReader(f))
    revision = revisar_base(filas, cargar_supresion())
    salida = ruta.with_name(f"{ruta.stem} · verificada.csv")
    with open(salida, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]) + ["decision", "categoria", "motivo", "proveedor_receptor"])
        w.writeheader()
        for fila, r in zip(filas, revision):
            w.writerow(fila | {"decision": r["decision"], "categoria": r["categoria"], "motivo": r["motivo"],
                               "proveedor_receptor": r["proveedor"]})
    cuenta: dict[str, int] = {}
    for r in revision:
        cuenta[f"{r['decision']} · {r['motivo']}"] = cuenta.get(f"{r['decision']} · {r['motivo']}", 0) + 1
    for k, n in sorted(cuenta.items(), key=lambda kv: -kv[1]):
        print(f"{n:5}  {k}")
    print(f"→ {salida}")


def main(args: list[str]) -> None:
    if args[:1] == ["verificar"] and len(args) == 2:
        _cmd_verificar(Path(args[1]))
    elif args == ["supresion"]:
        sup = construir_supresion(_fotos(), cargar_supresion())
        RUTA_SUPRESION.write_text(json.dumps(sup, ensure_ascii=False, indent=1))
        motivos: dict[str, int] = {}
        for v in sup.values():
            motivos[v["motivo"]] = motivos.get(v["motivo"], 0) + 1
        print(f"{len(sup)} correos en supresión → {RUTA_SUPRESION}  {motivos}")
    elif args == ["salud"]:
        datos = json.loads((DATOS / "ultimo.json").read_text())
        for nombre, s in salud_por(datos, lambda b, e, l: l["am"].get("secuencia")).items():
            pct = lambda v: "sin datos" if v is None else f"{v:.1%}"
            print(f"{s['nivel']:11} {nombre[:46]:46} contactados {s['contactados']:4} · rebote duro "
                  f"{pct(s['tasa_rebote_duro']):>9} · bloqueo {pct(s['tasa_bloqueo_politica']):>9} · "
                  f"respuesta {pct(s['tasa_respuesta']):>9}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
