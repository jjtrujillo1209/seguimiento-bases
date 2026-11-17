"""Arma la base de 50 empresas para cada seller de equipo.json que no tiene base cargada.

Uso:  .venv/bin/python armar_bases.py            # escribe los CSV en ~/Downloads/Bases sellers sin base <fecha>/
      .venv/bin/python armar_bases.py --simular  # solo informa, no escribe

Pasos:
1. Seller sin base = ningún contacto suyo en las bases que sigue la app (datos/ultimo.json).
2. Pool por segmento, de las bases ya prospectadas:
   · su propia base S46 si existe (~/Downloads/Bases S46 Enterprise/Base S46 · <nombre>.csv), filtrada a su segmento;
   · prospeccion-2000: base_2000 + decisores de base_2000_validada_amplemarket (+ correos de la revelada);
   · SMB: SMB_disponibles_no_tratadas (aptas, sin responsable) con decisores de base_2000_validada.
   Segmento por empleados: SMB < 300 · Mid Market 300–999 · Corporate 1.000+.
   Región del seller (MX) → solo empresas de México; Global o sin región → cualquiera.
3. Validación: fuera todo dominio ya asignado (bases de la app, todas las S46, repartos Platzi_300/Platzi_base,
   cockpit, lote-500) y, en HubSpot (solo lectura), las empresas Customer, con deal abierto o con
   actividad de ventas en los últimos 90 días.
4. Reparto por puntaje ICP, alternando entre sellers del mismo segmento para que la calidad sea pareja.
"""
import csv
import glob
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

import segmentos
from amplemarket import DOMINIOS_GENERICOS, lunes_iso, normalizar_dominio
from hubspot import HubSpot

RAIZ = Path(__file__).parent
load_dotenv(RAIZ / ".env")
DESCARGAS = Path.home() / "Downloads"
P2000 = RAIZ.parent / "prospeccion-2000"
S46 = DESCARGAS / "Bases S46 Enterprise"
HOY = date.today()
SEMANA = f"S{HOY.isocalendar()[1]}"
SEMANA_INICIO = lunes_iso(HOY.isocalendar()[0], HOY.isocalendar()[1]).isoformat()


def tag(seller_nombre: str) -> str:
    """Etiqueta semanal de trazabilidad: «S46.Stephania Orozco». Va en el nombre del archivo y en cada fila."""
    return f"{SEMANA}.{seller_nombre}"
# --semanal: base de la semana para quien cumplió (secuencia activa + tratamiento); sin flag: sellers sin base
SEMANAL = "--semanal" in sys.argv
UMBRAL_TRATAMIENTO = 0.8  # mínimo de empresas contactadas de su última base para recibir la siguiente
SALIDA = DESCARGAS / (f"Bases {SEMANA}" if SEMANAL else f"Bases sellers sin base {HOY.isoformat()}")
POR_SELLER = 50
DIAS_ACTIVIDAD = 90
ETIQUETA = {"smb": "SMB", "mid_market": "MID MARKET", "corporate": "CORPORATE", "sdr_ent": "CORPORATE", "gobierno": "GOBIERNO"}
# nombre del archivo S46 cuando difiere del de equipo.json
ALIAS_S46 = {"vicky.carroll@platzi.com": "Vicky Carroll"}

COLUMNAS = ["Tag", "Semana", "Semana inicio", "Responsable", "Empresa", "company", "company_name", "Dominio", "Segmento", "Empleados", "Industria", "País",
            "Ciudad", "Decisor", "Nombre", "first_name", "Cargo", "Email", "Estado email", "Celular", "LinkedIn",
            "Fuente", "HubSpot empresa", "Puntaje ICP"]


def leer(ruta) -> list[dict]:
    with open(ruta, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def num(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def segmento_por_empleados(n: float) -> str:
    return "smb" if n < 300 else "mid_market" if n < 1000 else "corporate"


def dominios_de_fila(fila: dict) -> set[str]:
    out = set()
    for k, v in fila.items():
        if not v or not k:
            continue
        kl = k.lower()
        if "dominio" in kl or "domain" in kl:
            d = normalizar_dominio(v)
            if d:
                out.add(d)
        elif "mail" in kl:
            for m in re.findall(r"[\w.+-]+@([\w-]+\.[\w.-]+)", v):
                if m.lower() not in DOMINIOS_GENERICOS:
                    out.add(m.lower())
    return out


def elegibles(equipo: list[dict]) -> list[dict]:
    """Regla de Full Stack Sellers: base nueva solo para quien tiene al menos una secuencia activa y
    contactó el UMBRAL_TRATAMIENTO de las empresas de su última base seguida."""
    datos = json.loads((RAIZ / "datos" / "ultimo.json").read_text())
    out = []
    for s in equipo:
        bases = sorted((b for b in datos["bases"] if (b.get("rep_email") or "").lower() == s["email"] and b["resumen"]["empresas"]),
                       key=lambda b: b["semana_clave"], reverse=True)
        activa = any(x["estado"] == "active" and (x.get("rep") == s["email"] or s["email"] in (x.get("reps") or []))
                     for x in datos.get("secuencias", []))
        if bases and activa and bases[0]["resumen"]["contactadas"] / bases[0]["resumen"]["empresas"] >= UMBRAL_TRATAMIENTO \
                and s["segmento"] in ETIQUETA:
            out.append(s)
    return out


def etiqueta_fila(seller: dict, empleados) -> str:
    """Segmento que va en la fila: el del seller; para SDR ENT, el de la empresa (Mid Market o Corporate)."""
    if seller["segmento"] == "sdr_ent":
        return ETIQUETA[segmento_por_empleados(num(empleados))]
    return ETIQUETA[seller["segmento"]]


def sin_base(equipo: list[dict]) -> list[dict]:
    datos = json.loads((RAIZ / "datos" / "ultimo.json").read_text())
    con = {(b.get("rep_email") or "").lower() for b in datos["bases"] if sum(len(e["leads"]) for e in b["empresas"])}
    return [s for s in equipo if s["email"] not in con and s["segmento"] in ETIQUETA]


def dominios_asignados(propios: set[Path], responsables_propios: set[str] = frozenset()) -> dict[str, str]:
    """dominio → dónde ya está asignado. Las filas de los sellers que estamos armando (su S46 propia,
    también dentro de «Todos los reps») no cuentan como asignadas a otro."""
    usado: dict[str, str] = {}
    datos = json.loads((RAIZ / "datos" / "ultimo.json").read_text())
    for b in datos["bases"]:
        for e in b["empresas"]:
            for d in [e.get("dominio")] + [l["email"].split("@")[1] for l in e["leads"] if "@" in (l.get("email") or "")]:
                if d and d not in DOMINIOS_GENERICOS:
                    usado.setdefault(normalizar_dominio(d), f"base en seguimiento ({b['rep_nombre']} {b['semana_clave']})")
    patrones = [S46 / "*.csv", DESCARGAS / "Platzi_300_*.csv", DESCARGAS / "Platzi_base_*.csv",
                DESCARGAS / "Platzi_bases_4_reps_general.csv", DESCARGAS / "cockpit_*.csv",
                DESCARGAS / "Andrea segmentacion Gondola*.csv",
                RAIZ.parent / "lote-500-latam" / "entregable_*.csv", RAIZ.parent / "lote-500-latam" / "500_*.csv",
                DESCARGAS / "Bases sellers sin base *" / "S*.*.csv", DESCARGAS / "Bases S[0-9]*" / "S*.*.csv"]  # bases ya entregadas por este script
    for patron in patrones:
        for ruta in glob.glob(str(patron)):
            if Path(ruta) in propios:
                continue
            nombre = Path(ruta).name
            for fila in leer(ruta):
                if (fila.get("Responsable") or "") in responsables_propios:
                    continue
                for d in dominios_de_fila(fila):
                    usado.setdefault(d, nombre)
    return usado


def decisores_2000() -> dict[str, list[dict]]:
    correos = {}
    for r in leer(P2000 / "base_2000_revelada_calificadas.csv"):
        if r.get("dm_email") and r.get("dm_email_estado") in ("deliverable", "risky", ""):
            correos[(r.get("dm_linkedin") or "").rstrip("/").lower() or r["dm_nombre"].lower()] = (r["dm_email"], r.get("dm_email_estado"))
    out = {}
    for r in leer(P2000 / "base_2000_validada_amplemarket.csv"):
        d = normalizar_dominio(r["dominio"])
        dms = []
        for i in ("1", "2"):
            nombre, li = r.get(f"dm{i}_nombre"), r.get(f"dm{i}_linkedin")
            if not nombre or not li:
                continue
            email, estado = correos.get(li.rstrip("/").lower()) or correos.get(nombre.lower()) or ("", "")
            dms.append({"Nombre": nombre, "first_name": nombre.split()[0], "Cargo": r.get(f"dm{i}_cargo"),
                        "LinkedIn": li, "Email": email, "Estado email": estado, "Celular": r.get(f"dm{i}_telefono") or ""})
        if dms:
            out[d] = dms
    return out


def pools(decisores) -> dict[str, list[dict]]:
    """segmento → empresas candidatas {dominio, Empresa, Empleados, País, …, decisores}."""
    info = {normalizar_dominio(r["dominio"]): r for r in leer(P2000 / "base_2000_empresas_icp_platzi_no_hubspot.csv")}
    out = {"smb": [], "mid_market": [], "corporate": []}
    for d, r in info.items():
        if d not in decisores:
            continue
        seg = segmento_por_empleados(num(r["empleados"]))
        out[seg].append({"dominio": d, "Empresa": r["empresa"], "Empleados": r["empleados"], "Industria": r["industria"],
                         "País": r["pais"], "Ciudad": r["ciudad"], "Puntaje ICP": r["puntaje_icp"],
                         "Fuente": "prospeccion-2000 · base_2000 + decisores Amplemarket", "decisores": decisores[d]})
    vistos = {c["dominio"] for c in out["smb"]}
    for r in leer(DESCARGAS / "SMB_disponibles_no_tratadas_2026-11-10.csv"):
        d = normalizar_dominio(r["dominio"])
        if r.get("apta_para_prospectar") != "Sí" or r.get("responsable_en_base") or d in vistos or d not in decisores:
            continue
        if num(r["empleados"]) >= 300:
            continue
        vistos.add(d)
        out["smb"].append({"dominio": d, "Empresa": r["empresa"], "Empleados": r["empleados"], "Industria": r["industria"],
                           "País": r["pais"], "Ciudad": r["ciudad"], "Puntaje ICP": "",
                           "Fuente": "SMB_disponibles_no_tratadas 2026-11-10 + decisores prospeccion-2000",
                           "decisores": decisores[d]})
    return out


def propia_s46(seller) -> tuple[Path | None, list[dict]]:
    """Su base S46 ya preparada, filtrada a su segmento, agrupada por empresa."""
    nombre = ALIAS_S46.get(seller["email"], seller["nombre"])
    ruta = S46 / f"Base S46 · {nombre}.csv"
    if not ruta.exists():
        return None, []
    empresas: dict[str, dict] = {}
    for r in leer(ruta):
        if r.get("Segmento") not in ({"MID MARKET", "CORPORATE"} if seller["segmento"] == "sdr_ent" else {ETIQUETA[seller["segmento"]]}):
            continue
        d = normalizar_dominio(r.get("Dominio")) or next(iter(dominios_de_fila(r)), "")
        if not d:
            continue
        e = empresas.setdefault(d, {"dominio": d, "Empresa": r["Empresa"], "Empleados": r["Empleados"],
                                    "Industria": r["Industria"], "País": r["País"], "Ciudad": r["Ciudad"],
                                    "Puntaje ICP": r.get("Puntaje ICP", ""), "Fuente": f"Base S46 · {nombre} (preparada)",
                                    "decisores": []})
        e["decisores"].append({k: r.get(k, "") for k in ("Nombre", "first_name", "Cargo", "LinkedIn", "Email", "Estado email", "Celular")})
    return ruta, list(empresas.values())


def validar_hubspot(dominios: list[str]) -> dict[str, tuple[bool, str]]:
    """dominio → (apta, texto). Solo lectura."""
    hs = HubSpot(os.getenv("HUBSPOT_PRIVATE_APP_TOKEN", "").strip())
    props = ["name", "domain", "lifecyclestage", "hs_num_open_deals", "hs_last_sales_activity_timestamp",
             "notes_last_contacted", "hubspot_owner_id"]
    owners = {o["id"]: " ".join(x for x in [o.get("firstName"), o.get("lastName")] if x) or o.get("email") for o in hs.owners()}
    # Solo dominio exacto: estas fuentes ya se cruzaron por nombre y variantes al construirse;
    # las variantes de país multiplicaban las consultas por ~15.
    encontrados: dict[str, list[dict]] = {}
    for c in hs.buscar_en("companies", "domain", dominios, props):
        dom = (c["properties"].get("domain") or "").lower()
        encontrados.setdefault(dom, []).append(c["properties"])
    limite = datetime.now(timezone.utc) - timedelta(days=DIAS_ACTIVIDAD)
    res = {}
    for d in dominios:
        cs = encontrados.get(d, [])
        if not cs:
            res[d] = (True, "No está en HubSpot")
            continue
        motivo = None
        for p in cs:
            ultima = max((p.get(k) or "" for k in ("hs_last_sales_activity_timestamp", "notes_last_contacted")), default="")
            reciente = ultima and datetime.fromisoformat(ultima.replace("Z", "+00:00")) >= limite
            if (p.get("lifecyclestage") or "") == "customer":
                motivo = f"Customer en HubSpot ({p.get('name')})"
            elif num(p.get("hs_num_open_deals")) > 0:
                motivo = f"Deal abierto ({p.get('name')}, owner {owners.get(p.get('hubspot_owner_id') or '', 'sin owner')})"
            elif reciente:
                motivo = f"Actividad de ventas el {ultima[:10]} ({owners.get(p.get('hubspot_owner_id') or '', 'sin owner')})"
            if motivo:
                break
        res[d] = (False, motivo) if motivo else (True, f"«{cs[0].get('name')}» está en HubSpot sin actividad de ventas reciente")
    return res


def main(simular=False):
    equipo = segmentos.cargar_equipo()
    sellers = elegibles(equipo) if SEMANAL else sin_base(equipo)
    print(f"{len(sellers)} sellers {'que cumplieron el tratamiento' if SEMANAL else 'sin base'}: " + ", ".join(s["nombre"] for s in sellers))
    propias = {s["email"]: propia_s46(s) for s in sellers}
    usado = dominios_asignados({r for r, _ in propias.values() if r},
                               {ALIAS_S46.get(s["email"], s["nombre"]) for s in sellers if propias[s["email"]][0]})
    decisores = decisores_2000()
    pool = pools(decisores)

    # candidatos de cada seller: primero su S46 propia, luego el pool de su segmento
    candidatos = {s["email"]: [e for e in propias[s["email"]][1] if e["dominio"] not in usado] for s in sellers}
    todos = {e["dominio"] for es in candidatos.values() for e in es} | {e["dominio"] for es in pool.values() for e in es if e["dominio"] not in usado}
    print(f"Validando {len(todos)} dominios candidatos en HubSpot…")
    hs = validar_hubspot(sorted(todos))
    excluidas = [{"Dominio": d, "Motivo": t} for d, (ok, t) in hs.items() if not ok]

    tomados: set[str] = set()
    asignacion = {s["email"]: [] for s in sellers}
    for s in sellers:  # su S46 propia primero (también respeta la región del seller)
        for e in candidatos[s["email"]]:
            if s.get("region") == "MX" and e["País"] != "Mexico":
                continue
            if len(asignacion[s["email"]]) < POR_SELLER and hs[e["dominio"]][0] and e["dominio"] not in tomados:
                tomados.add(e["dominio"]); asignacion[s["email"]].append(e)
    pool["sdr_ent"] = pool["mid_market"] + pool["corporate"]  # SDR ENT: empresas Enterprise (300+)
    for seg in ("corporate", "mid_market", "smb", "sdr_ent"):
        grupo = [s for s in sellers if s["segmento"] == seg]
        lista = sorted((e for e in pool[seg] if e["dominio"] not in usado and hs.get(e["dominio"], (False,))[0]),
                       key=lambda e: num(e["Puntaje ICP"]), reverse=True)
        while any(len(asignacion[s["email"]]) < POR_SELLER for s in grupo):
            avanzo = False
            for s in grupo:  # por turnos: la mejor disponible para cada uno
                if len(asignacion[s["email"]]) >= POR_SELLER:
                    continue
                solo_mx = s.get("region") == "MX"
                e = next((e for e in lista if e["dominio"] not in tomados and (not solo_mx or e["País"] == "Mexico")), None)
                if e:
                    tomados.add(e["dominio"]); asignacion[s["email"]].append(e); avanzo = True
            if not avanzo:
                break

    resumen = []
    for s in sellers:
        filas = []
        for e in asignacion[s["email"]]:
            for i, dm in enumerate(e["decisores"][:2], 1):
                filas.append({"Tag": tag(s["nombre"]), "Semana": SEMANA, "Semana inicio": SEMANA_INICIO, "Responsable": s["nombre"], "Empresa": e["Empresa"], "company": e["Empresa"], "company_name": e["Empresa"],
                              "Dominio": e["dominio"], "Segmento": etiqueta_fila(s, e["Empleados"]), "Empleados": e["Empleados"],
                              "Industria": e["Industria"], "País": e["País"], "Ciudad": e["Ciudad"], "Decisor": i,
                              **{k: dm.get(k, "") for k in ("Nombre", "first_name", "Cargo", "Email", "Estado email", "Celular", "LinkedIn")},
                              "Fuente": e["Fuente"], "HubSpot empresa": hs[e["dominio"]][1], "Puntaje ICP": e["Puntaje ICP"]})
        resumen.append({"Seller": s["nombre"], "Correo": s["email"], "Segmento": segmentos.SEGMENTOS[s["segmento"]],
                        "Equipo": s.get("equipo") or "", "Región": s.get("region") or "", "Líder": s.get("lider") or "",
                        "Empresas": len(asignacion[s["email"]]), "Decisores": len(filas),
                        "Con email": sum(bool(f["Email"]) for f in filas), "Con LinkedIn": sum(bool(f["LinkedIn"]) for f in filas),
                        "De su base S46": sum(e["Fuente"].startswith("Base S46") for e in asignacion[s["email"]]),
                        "Faltan para 50": max(POR_SELLER - len(asignacion[s["email"]]), 0)})
        if simular:
            continue
        SALIDA.mkdir(exist_ok=True)
        with open(SALIDA / f"{tag(s['nombre'])}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNAS); w.writeheader(); w.writerows(filas)
        # El CSV de importación lo escribe solo enriquecer_bases.py, después del filtro anti-rebote: aquí los correos
        # son candidatos (validaciones viejas, risky incluidos) y no deben llegar a Amplemarket.
        (SALIDA / f"{tag(s['nombre'])} · Importar Amplemarket.csv").unlink(missing_ok=True)
    for r in resumen:
        print(f"  {r['Seller']:<20} {r['Segmento']:<11} {r['Empresas']:>3} empresas · {r['Decisores']:>3} decisores · "
              f"{r['Con email']:>3} con email · {r['De su base S46']:>2} de su S46 · faltan {r['Faltan para 50']}")
    print(f"Pool por segmento (antes de validar): " + ", ".join(f"{k} {len(v)}" for k, v in pool.items() if k != "sdr_ent"))
    print(f"Excluidas por HubSpot: {len(excluidas)} · dominios ya asignados en otras bases: {len(usado)}")
    if not simular:
        for nombre, filas in (("Resumen", resumen), ("Excluidas por HubSpot", excluidas)):
            if filas:
                with open(SALIDA / f"{nombre}.csv", "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.DictWriter(f, fieldnames=list(filas[0])); w.writeheader(); w.writerows(filas)
        print(f"Archivos en {SALIDA}")
        print("Siguiente paso: .venv/bin/python enriquecer_bases.py" + (" --semanal" if SEMANAL else "")
              + "  (filtra el rebote y escribe el CSV de importación)")


if __name__ == "__main__":
    main(simular="--simular" in sys.argv)
