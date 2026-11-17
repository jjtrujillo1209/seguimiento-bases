"""Filtro anti-rebote de las bases semanales: ningún correo entra al CSV de importación sin pasarlo.

Lo usa enriquecer_bases.py (el único paso que escribe «· Importar Amplemarket.csv») y se puede correr
sobre cualquier CSV antes de importarlo:

    .venv/bin/python filtro_rebote.py                      # diagnóstico: rebote por base y por cohorte de filtro
    .venv/bin/python filtro_rebote.py verificar base.csv   # escribe «base · apto.csv» y «base · descartados.csv»
    .venv/bin/python filtro_rebote.py --subir-exclusion    # rebotes y dominios bloqueados → exclusión de Amplemarket

Un correo pasa solo si cumple TODO (fail-closed: si un dato no se pudo obtener, no pasa):
 1. Sintaxis válida y, si la fila trae estado de Amplemarket, «deliverable». Fuera risky, unknown,
    undeliverable y sin validar.
 2. No está suprimido: no rebotó ni se dio de baja en ninguna secuencia seguida (todas las fotos de
    datos/snapshots) y HubSpot no lo marca como hard bounce, dirección inválida o en cuarentena.
 3. Dominio sano: sin «spam_blocked», sin rebotes de otros contactos (REBOTES_POR_DOMINIO) y con MX
    consultado y presente (caché de 30 días en datos/cache_mx.json).
 4. Revalidación en lote de no más de REVALIDAR_DIAS días: «deliverable» y NO catch-all. Es obligatoria:
    sin ella el correo no se importa. Con ZEROBOUNCE_API_KEY en .env la hace ZeroBounce (solo «valid» pasa);
    si no, Amplemarket (/email-validations). 1 crédito por correo en ambos. El resultado se guarda en
    datos/validaciones/ y lo ya validado (o un lote de Amplemarket interrumpido) no se vuelve a pagar.

Por qué así (medido el 17-nov-2026 cruzando el estado de validación de origen con el resultado real):
«deliverable» del enriquecimiento rebotó 3,9 % (13 de 330), concentrado en servidores propios (9,3 %) y
Microsoft 365 (3,9 %), con 0 de 57 en Google: el patrón de los dominios catch-all, que solo detecta la
revalidación. «risky» rebotó 25 % (6 de 24). Si un contacto de un dominio rebotó, el otro rebotó 18 % de
las veces (3 de 17) contra 4,7 % general. Ninguna validación garantiza 0 %: el filtro deja fuera todo el
riesgo que se puede ver antes de enviar, y el diagnóstico mide lo que aun así rebote.
"""
import collections
import csv
import glob
import json
import os
import sys
import time
from datetime import date
from functools import lru_cache
from pathlib import Path

import requests
from dotenv import load_dotenv

import entregabilidad as en

RAIZ = Path(__file__).parent
load_dotenv(RAIZ / ".env")
API = "https://api.amplemarket.com"
REBOTE = {"hard_bounced", "email_unavailable", "spam_blocked", "error"}
BAJA = {"unsubscribed"}
PROPS_HS = ["email", "hs_email_hard_bounce_reason_enum", "hs_email_hard_bounce_reason", "hs_email_bad_address",
            "hs_email_quarantined", "hs_email_bounce"]
VALIDACIONES = RAIZ / "datos" / "validaciones"
REVALIDAR_DIAS = 30          # una revalidación más vieja se vuelve a pagar
REBOTES_POR_DOMINIO = 1      # rebotes de otros contactos del dominio que lo dejan fuera
MARCA_OK = "apto · deliverable · no catch-all · revalidado"
BASES_ENTREGADAS = ["Bases S[0-9]*/S*.*.csv", "Bases sellers sin base */S*.*.csv"]


def _dominio(email: str) -> str:
    return email.split("@")[1].lower() if "@" in (email or "") else ""


@lru_cache(maxsize=1)
def _fotos() -> tuple:
    """(correo, estado en la secuencia) de todas las fotos guardadas, leídas una sola vez por corrida."""
    filas = []
    for f in sorted(glob.glob(str(RAIZ / "datos" / "snapshots" / "*.json"))) + [str(RAIZ / "datos" / "ultimo.json")]:
        try:
            d = json.loads(Path(f).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for b in d.get("bases", []):
            for e in b.get("empresas", []):
                for l in e.get("leads", []):
                    m, am = (l.get("email") or "").lower(), l.get("am") or {}
                    if m and am.get("estado"):
                        filas.append((m, am["estado"], bool(am.get("primer_contacto"))))
    return tuple(filas)


def historial() -> tuple[set[str], dict[str, collections.Counter]]:
    """(correos que rebotaron alguna vez, {dominio: Counter(tipo de rebote → correos distintos)})."""
    correos, por_dominio = set(), collections.defaultdict(collections.Counter)
    for m, estado, _ in _fotos():
        if estado in REBOTE and m not in correos:
            correos.add(m)
            por_dominio[_dominio(m)][estado] += 1
    return correos, por_dominio


def bajas() -> set[str]:
    return {m for m, estado, _ in _fotos() if estado in BAJA}


def marcas_hubspot(emails: list[str]) -> dict[str, str]:
    """correo → motivo, para los que HubSpot ya marcó como rebote / inválido / cuarentena."""
    from hubspot import HubSpot
    if not emails:
        return {}
    hs = HubSpot(os.getenv("HUBSPOT_PRIVATE_APP_TOKEN", "").strip())
    out = {}
    for c in hs.buscar_en("contacts", "email", sorted(set(emails)), PROPS_HS):
        p = c["properties"]
        motivo = next((f"HubSpot: {k}" for k in PROPS_HS[1:] if p.get(k) not in (None, "", "false")), None)
        if motivo:
            out[(p.get("email") or "").lower()] = motivo
    return out


def _vigente(v: dict | None, hoy: date) -> bool:
    try:
        return bool(v) and (hoy - date.fromisoformat(v["fecha"])).days <= REVALIDAR_DIAS
    except (KeyError, TypeError, ValueError):
        return False  # resultados guardados sin fecha: se tratan como vencidos


def _sesion():
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {os.getenv('AMPLEMARKET_API_KEY', '').strip()}"})
    return s


def _recoger(s, lote: str, espera: float = 20, vueltas: int = 180) -> list[dict] | None:
    """Resultados de un lote (todas las páginas) cuando termina; None si sigue procesando al agotar la espera."""
    for _ in range(vueltas):
        d = s.get(f"{API}/email-validations/{lote}?page[size]=100", timeout=120).json()
        if d.get("status") == "completed":
            resultados, sig = list(d.get("results", [])), (d.get("_links", {}).get("next") or {}).get("href")
            while sig:
                p = s.get(API + sig, timeout=120).json()
                resultados += p.get("results", [])
                sig = (p.get("_links", {}).get("next") or {}).get("href")
            return resultados
        if d.get("status") in ("canceled", "error"):
            raise RuntimeError(f"La validación en lote {lote} terminó en «{d.get('status')}».")
        time.sleep(espera)
    return None


ZB_API, ZB_BULK, ZB_TRAMO = "https://api.zerobounce.net/v2", "https://bulkapi.zerobounce.net/v2", 100
# ZeroBounce → el mismo formato que Amplemarket (result + catch_all). Solo «valid» queda deliverable y no catch-all.
ZB_RESULTADO = {"valid": ("deliverable", False), "catch-all": ("risky", True), "invalid": ("undeliverable", False),
                "spamtrap": ("undeliverable", False), "abuse": ("undeliverable", False),
                "do_not_mail": ("undeliverable", False), "unknown": ("unknown", False)}


def _zerobounce(nuevos: list[str], guardar, sesion=None, clave: str | None = None) -> None:
    """Valida con ZeroBounce (validatebatch, síncrono, 1 crédito por correo). Revisa el saldo antes de cobrar y
    guarda cada tramo apenas vuelve, así una caída a mitad no hace pagar otra vez lo ya validado."""
    s, clave = sesion or requests.Session(), clave or os.getenv("ZEROBOUNCE_API_KEY", "").strip()
    saldo = float(s.get(f"{ZB_API}/getcredits", params={"api_key": clave}, timeout=60).json().get("Credits", -1))
    if saldo < 0:
        raise RuntimeError("ZeroBounce: la llave no es válida (getcredits = -1). No se cobró nada.")
    if saldo < len(nuevos):
        raise RuntimeError(f"ZeroBounce: {saldo:.0f} créditos para {len(nuevos)} correos. No se cobró nada.")
    def convertir(x):
        res, catch_all = ZB_RESULTADO.get(x.get("status"), ("unknown", False))
        return {"email": x.get("address"), "result": res, "catch_all": catch_all, "fuente": "zerobounce",
                "detalle": "/".join(filter(None, (x.get("status"), x.get("sub_status"))))}

    lote_ok = True
    for i in range(0, len(nuevos), ZB_TRAMO):
        tramo = nuevos[i:i + ZB_TRAMO]
        if lote_ok:
            r = s.post(f"{ZB_BULK}/validatebatch", timeout=300, json={
                "api_key": clave, "email_batch": [{"email_address": e, "ip_address": ""} for e in tramo]})
            if r.status_code == 403:  # cuentas sin acceso a bulkapi: correo por correo en la API principal
                lote_ok = False
            else:
                r.raise_for_status()
                d = r.json()
                if d.get("errors"):
                    raise RuntimeError(f"ZeroBounce devolvió errores: {d['errors'][:3]}")
                guardar([convertir(x) for x in d.get("email_batch", [])])
                continue
        for e in tramo:
            r = s.get(f"{ZB_API}/validate", params={"api_key": clave, "email": e, "ip_address": ""}, timeout=120)
            r.raise_for_status()
            x = r.json()
            if x.get("error"):
                raise RuntimeError(f"ZeroBounce: {x['error']}")
            guardar([convertir(x)])


def revalidar(emails: list[str], sesion=None, carpeta: Path = VALIDACIONES, hoy: date | None = None,
              espera: float = 20, proveedor: str | None = None) -> dict[str, dict]:
    """Segunda validación: {correo: {result, catch_all, fecha}} solo de resultados vigentes. Paga 1 crédito por
    correo sin resultado vigente. Proveedor: ZeroBounce si hay ZEROBOUNCE_API_KEY, si no Amplemarket (que primero
    retoma los lotes que quedaron pendientes)."""
    if proveedor is None:
        proveedor = "zerobounce" if sesion is None and os.getenv("ZEROBOUNCE_API_KEY", "").strip() else "amplemarket"
    hoy = hoy or date.today()
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / "resultados.json"
    hechos = json.loads(ruta.read_text()) if ruta.exists() else {}

    def guardar(resultados):
        for x in resultados:
            hechos[(x.get("email") or "").lower()] = {"result": x.get("result"), "catch_all": bool(x.get("catch_all")),
                                                      "fecha": hoy.isoformat(), "fuente": x.get("fuente", "amplemarket")} \
                | ({"detalle": x["detalle"]} if x.get("detalle") else {})
        ruta.write_text(json.dumps(hechos, ensure_ascii=False))

    pedidos = {e.lower() for e in emails if e}
    if proveedor == "zerobounce":
        nuevos = sorted(e for e in pedidos if not _vigente(hechos.get(e), hoy))
        if nuevos:
            _zerobounce(nuevos, guardar, sesion)
        return {e: hechos[e] for e in pedidos if _vigente(hechos.get(e), hoy)}

    s = sesion or _sesion()
    for archivo in sorted(carpeta.glob("lote_*.json")):  # lotes pagados que no se alcanzaron a recoger
        lote = json.loads(archivo.read_text())
        if not lote.get("recogido"):
            resultados = _recoger(s, lote["id"], espera, vueltas=1)
            if resultados is not None:
                guardar(resultados)
                archivo.write_text(json.dumps(lote | {"recogido": hoy.isoformat()}))

    pedidos = {e.lower() for e in emails if e}
    nuevos = sorted(e for e in pedidos if not _vigente(hechos.get(e), hoy))
    if nuevos:
        r = s.post(API + "/email-validations", json={"emails": [{"email": e} for e in nuevos]}, timeout=120)
        if r.status_code == 402:
            raise RuntimeError("Amplemarket: créditos de validación insuficientes (402). No se cobró nada.")
        r.raise_for_status()
        lote = r.json()["id"]
        archivo = carpeta / f"lote_{lote}.json"
        archivo.write_text(json.dumps({"id": lote, "emails": nuevos, "pedido": hoy.isoformat()}))
        resultados = _recoger(s, lote, espera)
        if resultados is None:
            raise RuntimeError(f"La validación en lote {lote} sigue procesando; vuelve a correr más tarde "
                               "(se retoma ese mismo lote, no se cobra dos veces).")
        guardar(resultados)
        archivo.write_text(json.dumps(json.loads(archivo.read_text()) | {"recogido": hoy.isoformat()}))
    return {e: hechos[e] for e in pedidos if _vigente(hechos.get(e), hoy)}


class Filtro:
    """Evalúa un lote de correos de una vez (HubSpot, MX y la revalidación van por lotes).
    Los parámetros con nombre permiten inyectar los datos en las pruebas."""

    def __init__(self, emails: list[str], revalidar_lote: bool = True, *, rebotados=None, por_dominio=None,
                 suprimidos_baja=None, hubspot=None, mx=None, segunda=None):
        emails = sorted({e.strip().lower() for e in emails if e})
        if rebotados is None or por_dominio is None:
            rebotados, por_dominio = historial()
        self.rebotados, self.por_dominio = rebotados, por_dominio
        self.bajas = bajas() if suprimidos_baja is None else suprimidos_baja
        self.hubspot = marcas_hubspot(emails) if hubspot is None else hubspot
        self.mx = en.CacheMX().resolver({_dominio(e) for e in emails if _dominio(e)}) if mx is None else mx
        self.revalidado = revalidar_lote or segunda is not None
        self.segunda = segunda if segunda is not None else (revalidar(emails) if revalidar_lote else {})

    def evaluar(self, email: str, estado: str | None = None) -> tuple[bool, str]:
        """(pasa, motivo). `estado` es la validación de Amplemarket de la fila; None si el CSV no la trae
        (entonces decide la revalidación, que es la misma prueba más reciente)."""
        m = (email or "").strip().lower()
        dom = _dominio(m)
        if not m or not dom:
            return False, "sin correo"
        if not en.sintaxis_ok(m):
            return False, "sintaxis inválida"
        if estado is not None and (estado or "").lower() != "deliverable":
            return False, f"validación «{estado or 'sin validar'}» (solo pasa deliverable)"
        if m in self.bajas:
            return False, "se dio de baja en una secuencia"
        if m in self.rebotados:
            return False, "ya rebotó en una secuencia"
        if m in self.hubspot:
            return False, self.hubspot[m]
        hist = self.por_dominio.get(dom, collections.Counter())
        if hist["spam_blocked"]:
            return False, "dominio bloqueó nuestros correos (spam_blocked)"
        if hist["hard_bounced"] + hist["email_unavailable"] >= REBOTES_POR_DOMINIO:
            return False, f"dominio con {hist['hard_bounced'] + hist['email_unavailable']} rebote(s) de otros contactos"
        mx = self.mx.get(dom)
        if not mx or mx.get("estado") == "sin_consultar":
            return False, "no se pudo consultar el MX del dominio"
        if mx["estado"] in ("sin_mx", "no_existe"):
            return False, "el dominio no recibe correo (sin MX)"
        if not self.revalidado:
            return False, "pendiente de revalidación (sin ella no se importa)"
        v = self.segunda.get(m)
        if not v:
            return False, "no se pudo revalidar"
        if v.get("catch_all"):
            return False, "dominio catch-all (acepta todo y rebota después)"
        if v.get("result") != "deliverable":
            return False, f"revalidación «{v.get('result')}»"
        return True, MARCA_OK


# ── Verificación de un CSV cualquiera antes de importarlo ──────────────────────────────────────────
COLS_EMAIL = ("Email", "email", "Correo", "correo")
COLS_ESTADO = ("Estado email", "email_status", "Estado")


def verificar_csv(ruta: Path, revalidar_lote: bool = True, filtro: Filtro | None = None) -> dict:
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f)
        columnas, filas = list(lector.fieldnames or []), list(lector)
    col_email = next((c for c in COLS_EMAIL if c in columnas), None)
    if not col_email:
        raise RuntimeError(f"{ruta.name}: no tiene columna de correo ({', '.join(COLS_EMAIL)}).")
    col_estado = next((c for c in COLS_ESTADO if c in columnas), None)
    filtro = filtro or Filtro([r[col_email] for r in filas], revalidar_lote=revalidar_lote)
    aptas, descartes, vistos = [], [], set()
    for r in filas:
        m = (r[col_email] or "").strip().lower()
        ok, motivo = filtro.evaluar(m, r.get(col_estado) if col_estado else None)
        if ok and m in vistos:
            ok, motivo = False, "duplicado en el mismo archivo"
        if ok:
            vistos.add(m)
            aptas.append(r)
        else:
            descartes.append(r | {"Motivo descarte": motivo})
    salidas = {}
    for sufijo, filas_, cols in (("apto", aptas, columnas), ("descartados", descartes, columnas + ["Motivo descarte"])):
        salidas[sufijo] = ruta.with_name(f"{ruta.stem} · {sufijo}.csv")
        with open(salidas[sufijo], "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(filas_)
    return {"aptas": len(aptas), "descartadas": len(descartes), "salidas": salidas,
            "motivos": collections.Counter(d["Motivo descarte"].split(" (")[0] for d in descartes)}


def subir_exclusion() -> dict:
    """Agrega a las listas de exclusión de Amplemarket los correos que rebotaron y los dominios con spam_blocked.
    Solo agrega (nunca borra); recuerda lo ya subido en datos/exclusion_amplemarket.json; jamás excluye un
    dominio genérico (gmail, hotmail…)."""
    from amplemarket import DOMINIOS_GENERICOS
    correos, por_dominio = historial()
    dominios = {d for d, c in por_dominio.items() if c["spam_blocked"] and d not in DOMINIOS_GENERICOS}
    ruta = RAIZ / "datos" / "exclusion_amplemarket.json"
    hecho = json.loads(ruta.read_text()) if ruta.exists() else {"emails": [], "domains": []}
    s = _sesion()
    resultado = {}
    for tipo, ruta_api, campo, items in (("emails", "/excluded-emails", "email", sorted(correos - set(hecho["emails"]))),
                                         ("domains", "/excluded-domains", "domain", sorted(dominios - set(hecho["domains"])))):
        for i in range(0, len(items), 200):
            r = s.post(API + ruta_api, json={f"excluded_{tipo}": [{campo: x} for x in items[i:i + 200]]}, timeout=120)
            r.raise_for_status()
            estados = r.json()
            ok = [x for x, est in estados.items() if est in ("success", "duplicated")]
            hecho[tipo] = sorted(set(hecho[tipo]) | set(ok))
            resultado.setdefault(tipo, collections.Counter()).update(estados.values())
            ruta.write_text(json.dumps(hecho, ensure_ascii=False, indent=1))
    return {k: dict(v) for k, v in resultado.items()}


# ── Diagnóstico: ¿el filtro está dejando el rebote en 0? ───────────────────────────────────────────
def cohortes(fotos=None, archivos=None) -> dict[str, collections.Counter]:
    """Rebote real según cómo se filtró el correo al armar su base: «filtro completo» (revalidado),
    «filtro sin revalidar» (bases anteriores a este cambio) o «sin filtro». Denominador: correos con primer
    contacto o con un estado de rebote. Un correo cuenta una vez (su peor estado)."""
    marca = {}
    for patron in archivos if archivos is not None else [str(Path.home() / "Downloads" / p) for p in BASES_ENTREGADAS]:
        for f in glob.glob(patron):
            if "Importar" in f or "descartados" in f:
                continue
            with open(f, encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    m, filtro = (r.get("Email") or "").strip().lower(), r.get("Filtro rebote") or ""
                    if m:
                        nueva = "filtro completo" if "revalidado" in filtro else "filtro sin revalidar" if filtro else "sin filtro"
                        actual = marca.get(m, "sin filtro")
                        orden = ["sin filtro", "filtro sin revalidar", "filtro completo"]
                        marca[m] = max(actual, nueva, key=orden.index)
    estado = {}
    for m, est, contactado in fotos if fotos is not None else _fotos():
        if est in REBOTE:
            estado[m] = "rebote"
        elif contactado and m not in estado:
            estado[m] = "enviado"
    out = collections.defaultdict(collections.Counter)
    for m, r in estado.items():
        c = marca.get(m, "sin filtro")
        out[c][r] += 1
        if r == "rebote":
            out[c]["dominio:" + _dominio(m)] += 1
    return out


def diagnostico():
    d = json.loads((RAIZ / "datos" / "ultimo.json").read_text())
    tot = collections.Counter()
    print("Rebote por base (enviados = con primer contacto o rebotados):")
    for b in sorted(d["bases"], key=lambda b: (b["semana_clave"], b["rep_nombre"] or "")):
        ls = [l for e in b["empresas"] for l in e["leads"] if l.get("am")]
        env = [l for l in ls if l["am"].get("primer_contacto") or l["am"]["estado"] in REBOTE]
        reb = [l for l in ls if l["am"]["estado"] in REBOTE]
        if env:
            print(f"  {b['semana_clave']} {(b['rep_nombre'] or '')[:20]:<20} {len(reb):>3}/{len(env):<4} {100 * len(reb) / len(env):5.1f}%")
        tot.update(env=len(env), reb=len(reb))
    print(f"Total: {tot['reb']}/{tot['env']} = {100 * tot['reb'] / max(tot['env'], 1):.1f}%")

    print("\nRebote por cohorte de filtro (todas las fotos):")
    for nombre in ("filtro completo", "filtro sin revalidar", "sin filtro"):
        c = cohortes().get(nombre, collections.Counter())
        n = c["rebote"] + c["enviado"]
        tasa = f"{100 * c['rebote'] / n:5.1f}%" if n else "sin datos"
        print(f"  {nombre:<21} {c['rebote']:>3}/{n:<5} {tasa}")
        if nombre == "filtro completo" and c["rebote"]:
            doms = sorted((k[8:] for k in c if k.startswith("dominio:")))
            print(f"    ⚠ Rebotes que el filtro no vio, revisar a mano: {', '.join(doms)}")

    correos, dom = historial()
    print(f"\nSupresión: {len(correos)} correos que rebotaron · {len(bajas())} bajas · "
          f"{sum(1 for c in dom.values() if c['spam_blocked'])} dominios spam_blocked · "
          f"{sum(1 for c in dom.values() if c['hard_bounced'] + c['email_unavailable'] >= REBOTES_POR_DOMINIO)} "
          f"dominios con {REBOTES_POR_DOMINIO}+ rebotes")


if __name__ == "__main__":
    if "--subir-exclusion" in sys.argv:
        print(subir_exclusion())
    elif sys.argv[1:2] == ["verificar"] and len(sys.argv) >= 3:
        r = verificar_csv(Path(sys.argv[2]), revalidar_lote="--sin-revalidar" not in sys.argv)
        print(f"{r['aptas']} aptas · {r['descartadas']} descartadas · " + ", ".join(f"{m} {n}" for m, n in r["motivos"].most_common()))
        for k, ruta in r["salidas"].items():
            print(f"  {k}: {ruta}")
    else:
        diagnostico()
