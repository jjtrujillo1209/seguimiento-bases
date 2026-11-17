"""Enriquece con correo y celular las bases que armó armar_bases.py, usando listas de leads de Amplemarket.

Uso:  .venv/bin/python enriquecer_bases.py

Por seller crea UNA lista privada en Amplemarket (owner jtrujillo@, visible=false, nombre
«Enriquecimiento · <seller> · <fecha>», que la sincronización NO toma como base) con enrich +
validate_email + reveal_phone_numbers sobre los LinkedIn de sus decisores. Gasta créditos de
Amplemarket. Cada lista creada se guarda en datos/enriquecimiento/ y se reutiliza si se vuelve a
correr: nunca se paga dos veces el mismo lead.

Un decisor queda si tiene celular y su correo pasa filtro_rebote.py (deliverable, sin historial de rebote
ni baja, dominio sano con MX y revalidado en lote sin catch-all; la revalidación cuesta 1 crédito por correo
y es obligatoria). Es el único paso que escribe el CSV de importación. Si una empresa se queda sin
ningún decisor completo, se repone con la siguiente empresa del pool de su segmento (misma
validación de armar_bases.py) y se enriquece en otra ronda, hasta 50 empresas o 3 rondas.
"""
import csv
import json
import os
import re
import time
from pathlib import Path

import requests

import collections
import hashlib

import armar_bases as ab
import filtro_rebote
import segmentos

DUENO = "jtrujillo@platzi.com"
CACHE = ab.RAIZ / "datos" / "enriquecimiento"
RONDAS = 3
# La segunda validación en lote (1 crédito/correo, fuera catch-all) es obligatoria: sin ella ningún correo pasa
API = "https://api.amplemarket.com"


def _sesion():
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {os.getenv('AMPLEMARKET_API_KEY', '').strip()}",
                      "Content-Type": "application/json"})
    return s


def norm_li(url: str) -> str:
    u = (url or "").strip().lower().split("?")[0].rstrip("/")
    return re.sub(r"^https?://([a-z]{2,3}\.)?linkedin\.com", "linkedin.com", u)


def crear_lista(s, nombre: str, linkedins: list[str]) -> str:
    cuerpo = {"name": nombre, "shared": False, "visible": False, "owner": DUENO, "type": "linkedin",
              "options": {"reveal_phone_numbers": True, "validate_email": True, "enrich": True},
              "leads": [{"linkedin_url": u} for u in linkedins]}
    r = s.post(API + "/lead-lists", json=cuerpo, timeout=120)
    if r.status_code == 402:
        raise RuntimeError("Amplemarket: créditos insuficientes (402). No se cobró nada por esta lista.")
    if r.status_code >= 400:
        raise RuntimeError(f"Amplemarket {r.status_code}: {r.text[:300]}")
    return r.json()["id"]


def _get(s, url: str) -> dict:
    """GET tolerante: Amplemarket a veces tarda o corta mientras procesa la lista."""
    for intento in range(8):
        try:
            r = s.get(url, timeout=180)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(str(r.status_code))
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError):
            time.sleep(10 + intento * 10)
    raise RuntimeError(f"Amplemarket no respondió en {url}; vuelve a correr el script (las listas ya creadas no se cobran otra vez).")


def leer_lista(s, lista_id: str) -> dict:
    d = _get(s, f"{API}/lead-lists/{lista_id}?page[size]=100")
    sig = (d.get("_links", {}).get("next") or {}).get("href")
    while sig:
        p = _get(s, API + sig)
        d["leads"] = d.get("leads", []) + p.get("leads", [])
        sig = (p.get("_links", {}).get("next") or {}).get("href")
    return d


def asegurar_lista(s, clave: str, nombre: str, linkedins: list[str]) -> dict:
    """Crea la lista si no está en la caché (nunca dos veces) y devuelve su estado guardado."""
    CACHE.mkdir(parents=True, exist_ok=True)
    ruta = CACHE / f"{clave}.json"
    estado = json.loads(ruta.read_text()) if ruta.exists() else {}
    if not estado.get("id"):
        estado = {"id": crear_lista(s, nombre, linkedins), "nombre": nombre, "pedidos": len(linkedins)}
        ruta.write_text(json.dumps(estado, ensure_ascii=False))
        print(f"  lista creada: {nombre} ({len(linkedins)} leads)", flush=True)
    return estado


def esperar_listas(s, claves: list[str], horas: float = 6) -> None:
    """Espera a que TODAS las listas de la ronda terminen en Amplemarket (procesan en paralelo)."""
    limite = time.time() + horas * 3600
    while True:
        faltan = []
        for clave in claves:
            ruta = CACHE / f"{clave}.json"
            estado = json.loads(ruta.read_text())
            if estado.get("status") == "completed":
                continue
            d = leer_lista(s, estado["id"])
            if d.get("status") == "completed":
                estado.update(status="completed", leads=d.get("leads", []))
                ruta.write_text(json.dumps(estado, ensure_ascii=False))
                print(f"  terminada: {estado['nombre']} ({len(estado['leads'])} leads)", flush=True)
            else:
                faltan.append(estado["nombre"])
        if not faltan:
            return
        if time.time() > limite:
            raise RuntimeError(f"Siguen procesando en Amplemarket: {', '.join(faltan)}. Vuelve a correr el script más tarde.")
        print(f"  {time.strftime('%H:%M')} · {len(faltan)} listas procesando en Amplemarket…", flush=True)
        time.sleep(120)


def enriquecer(s, clave: str, nombre: str, linkedins: list[str]) -> dict[str, dict]:
    """linkedin normalizado → {email, estado_email, telefono}, de una lista ya terminada."""
    estado = asegurar_lista(s, clave, nombre, linkedins)
    if estado.get("status") != "completed":
        esperar_listas(s, [clave])
        estado = json.loads((CACHE / f"{clave}.json").read_text())
    # Amplemarket sigue sumando teléfonos después de marcar «completed»: se relee justo antes de usarla
    estado["leads"] = leer_lista(s, estado["id"]).get("leads", []) or estado.get("leads", [])
    (CACHE / f"{clave}.json").write_text(json.dumps(estado, ensure_ascii=False))
    out = {}
    for l in estado["leads"]:
        tels = l.get("phone_numbers") or []
        tels.sort(key=lambda t: 0 if (t.get("type") or "").lower() in ("mobile", "mobile_phone", "personal") else 1)
        out[norm_li(l.get("linkedin_url"))] = {
            "email": l.get("email") or "", "estado": (l.get("email_validation_result") or {}).get("result") or "",
            "telefono": tels[0]["number"] if tels else "", "tipo_tel": tels[0].get("type") if tels else "",
            "cargo": l.get("title") or "", "nombre": l.get("name") or " ".join(x for x in [l.get("first_name"), l.get("last_name")] if x),
        }
    return out


def completo(dm: dict) -> bool:
    """Tiene correo, celular y validación «deliverable» de Amplemarket (el resto lo decide filtro_rebote)."""
    return bool(dm.get("Email")) and dm.get("Estado email") == "deliverable" and bool(dm.get("Celular"))


def clave_lista(em: str, ronda: int, linkedins: list[str]) -> str:
    """Una lista por (semana, seller, ronda, leads pedidos): una corrida repetida reutiliza la misma y no paga dos veces."""
    h = hashlib.sha1("|".join(sorted(norm_li(u) for u in linkedins)).encode()).hexdigest()[:8]
    return f"{ab.SEMANA + '_' if ab.SEMANAL else ''}{em.split('@')[0]}_r{ronda}_{h}"


def main():
    s = _sesion()
    equipo = {x["email"]: x for x in segmentos.cargar_equipo()}
    archivos = sorted(p for p in ab.SALIDA.glob(f"{ab.SEMANA}.*.csv") if "Importar" not in p.name)
    # base actual por seller: empresas con sus decisores
    bases = {}
    for f in archivos:
        filas = ab.leer(f)
        seller = next(x for x in equipo.values() if x["nombre"] == filas[0]["Responsable"])
        empresas = {}
        for r in filas:
            e = empresas.setdefault(r["Dominio"], {k: r[k] for k in ("Empresa", "Empleados", "Industria", "País", "Ciudad", "Puntaje ICP", "Fuente", "HubSpot empresa")} | {"dominio": r["Dominio"], "decisores": []})
            e["decisores"].append({k: r[k] for k in ("Nombre", "first_name", "Cargo", "LinkedIn", "Email", "Estado email", "Celular")})
        bases[seller["email"]] = {"seller": seller, "empresas": list(empresas.values()), "archivo": f}

    # pool de reposición (ya validado igual que en armar_bases)
    usados = {d for b in bases.values() for d in (e["dominio"] for e in b["empresas"])}
    asignado = ab.dominios_asignados(set(), {b["seller"]["nombre"] for b in bases.values()})
    pool = ab.pools(ab.decisores_2000())
    pool["sdr_ent"] = pool["mid_market"] + pool["corporate"]  # SDR ENT: empresas Enterprise (300+)
    reposicion = {k: [e for e in sorted(v, key=lambda e: ab.num(e["Puntaje ICP"]), reverse=True)
                      if e["dominio"] not in asignado and e["dominio"] not in usados] for k, v in pool.items()}
    validado: dict[str, tuple] = {}
    tomadas: set[str] = set(usados)  # dominios ya en una base de esta corrida o pedidos para reponer

    pendientes = {em: b["empresas"] for em, b in bases.items()}  # empresas por enriquecer en esta ronda
    finales = {em: [] for em in bases}
    en_final: set[str] = set()      # una empresa nunca queda en dos sellers
    descartes: list[dict] = []      # decisores fuera por el filtro anti-rebote, con su motivo
    for ronda in range(1, RONDAS + 1):
        print(f"Ronda {ronda}: {sum(map(len, pendientes.values()))} empresas por enriquecer", flush=True)
        # primero se crean TODAS las listas de la ronda y se esperan juntas
        claves = []
        # se enriquece solo a quien le falta correo o celular; un correo no-deliverable no se vuelve a pagar: lo saca el filtro
        falta_dato = lambda dm: dm.get("LinkedIn") and not (dm.get("Email") and dm.get("Celular"))
        for em, empresas in pendientes.items():
            lis = [dm["LinkedIn"] for e in empresas for dm in e["decisores"] if falta_dato(dm)]
            if lis:
                clave = clave_lista(em, ronda, lis)
                asegurar_lista(s, clave, f"Enriquecimiento · {bases[em]['seller']['nombre']} · {ab.HOY.isoformat()}"
                               + (f" · ronda {ronda}" if ronda > 1 else ""), lis)
                claves.append(clave)
        esperar_listas(s, claves)
        for em, empresas in pendientes.items():
            if not empresas:
                continue
            seller = bases[em]["seller"]
            lis = [dm["LinkedIn"] for e in empresas for dm in e["decisores"] if falta_dato(dm)]
            res = enriquecer(s, clave_lista(em, ronda, lis),
                             f"Enriquecimiento · {seller['nombre']} · {ab.HOY.isoformat()}" + (f" · ronda {ronda}" if ronda > 1 else ""), lis) if lis else {}
            for e in empresas:
                for dm in e["decisores"]:
                    x = res.get(norm_li(dm.get("LinkedIn")))
                    if x and x["email"]:  # el correo que se usa es el que Amplemarket acaba de validar
                        dm["Email"] = x["email"]
                        dm["Estado email"] = x["estado"]
                    if x:
                        dm["Celular"] = dm.get("Celular") or x["telefono"]
                        dm["Cargo"] = dm.get("Cargo") or x["cargo"]
        # filtro anti-rebote sobre todos los correos completos de la ronda (HubSpot y revalidación por lote)
        candidatos = [dm["Email"] for es in pendientes.values() for e in es for dm in e["decisores"] if completo(dm)]
        filtro = filtro_rebote.Filtro(candidatos, revalidar_lote=True)
        for em, empresas in pendientes.items():
            for e in empresas:
                buenos = []
                for dm in e["decisores"]:
                    if not completo(dm):
                        continue
                    ok, motivo = filtro.evaluar(dm["Email"], dm["Estado email"])
                    if ok:
                        buenos.append(dm | {"Filtro rebote": motivo})
                    else:
                        descartes.append({"Seller": bases[em]["seller"]["nombre"], "Empresa": e["Empresa"], "Dominio": e["dominio"],
                                          "Nombre": dm.get("Nombre"), "Email": dm["Email"], "Estado email": dm["Estado email"], "Motivo": motivo})
                e["decisores"] = buenos[:2]
                if e["decisores"] and len(finales[em]) < ab.POR_SELLER and e["dominio"] not in en_final:
                    finales[em].append(e); en_final.add(e["dominio"])
        # reposición: lo que falta, con un margen porque no todas salen completas
        pendientes = {}
        for em, b in bases.items():
            falta = ab.POR_SELLER - len(finales[em])
            if falta <= 0 or ronda == RONDAS:
                continue
            seg = b["seller"]["segmento"]
            solo_mx = b["seller"].get("region") == "MX"
            cand = [e for e in reposicion[seg] if e["dominio"] not in tomadas and (not solo_mx or e["País"] == "Mexico")][: falta * 2]
            tomadas.update(e["dominio"] for e in cand)
            nuevos = [e["dominio"] for e in cand if e["dominio"] not in validado]
            if nuevos:
                validado.update(ab.validar_hubspot(nuevos))
            elegidas = []
            for e in cand:
                reposicion[seg].remove(e)
                if validado[e["dominio"]][0]:
                    elegidas.append(e | {"HubSpot empresa": validado[e["dominio"]][1], "Fuente": e["Fuente"] + " · reposición"})
            pendientes[em] = elegidas
        if not pendientes:
            break

    resumen = []
    for em, b in bases.items():
        s_ = b["seller"]
        filas = []
        for e in finales[em]:
            for i, dm in enumerate(e["decisores"], 1):
                filas.append({"Tag": ab.tag(s_["nombre"]), "Semana": ab.SEMANA, "Semana inicio": ab.SEMANA_INICIO, "Responsable": s_["nombre"], "Empresa": e["Empresa"], "company": e["Empresa"], "company_name": e["Empresa"],
                              "Dominio": e["dominio"], "Segmento": ab.etiqueta_fila(s_, e["Empleados"]), "Empleados": e["Empleados"],
                              "Industria": e["Industria"], "País": e["País"], "Ciudad": e["Ciudad"], "Decisor": i,
                              **{k: dm.get(k, "") for k in ("Nombre", "first_name", "Cargo", "Email", "Estado email", "Celular", "LinkedIn")},
                              "Fuente": e["Fuente"], "HubSpot empresa": e["HubSpot empresa"], "Puntaje ICP": e["Puntaje ICP"],
                              "Filtro rebote": dm.get("Filtro rebote", "")})
        # Candado: al CSV de importación solo llega lo que pasó el filtro completo
        sin_filtro = [r["Email"] for r in filas if r["Filtro rebote"] != filtro_rebote.MARCA_OK]
        if sin_filtro:
            raise RuntimeError(f"{len(sin_filtro)} correos de {s_['nombre']} no pasaron el filtro anti-rebote completo; "
                               "no se escribe su CSV de importación.")
        with open(b["archivo"], "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=ab.COLUMNAS + ["Filtro rebote"]); w.writeheader(); w.writerows(filas)
        with open(ab.SALIDA / f"{ab.tag(s_['nombre'])} · Importar Amplemarket.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["tag", "semana", "platzi_ref", "linkedin_url", "first_name", "last_name", "title", "company", "company_name", "company_domain", "email",
                        "phone_number", "country", "city", "responsable"])
            for r in filas:
                p = (r["Nombre"] or "").split()
                w.writerow([r["Tag"], ab.SEMANA, r["Tag"], r["LinkedIn"], p[0] if p else "", " ".join(p[1:]), r["Cargo"], r["Empresa"], r["Empresa"], r["Dominio"],
                            r["Email"], r["Celular"], r["País"], r["Ciudad"], em])
        resumen.append({"Seller": s_["nombre"], "Segmento": segmentos.SEGMENTOS[s_["segmento"]], "Líder": s_.get("lider") or "",
                        "Empresas": len(finales[em]), "Decisores": len(filas),
                        "Con correo y celular": sum(completo(r) for r in filas),
                        "Faltan para 50": max(ab.POR_SELLER - len(finales[em]), 0)})
        print(f"  {s_['nombre']:<20} {len(finales[em]):>3} empresas · {len(filas):>3} decisores con correo y celular", flush=True)
    with open(ab.SALIDA / "Resumen.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(resumen[0])); w.writeheader(); w.writerows(resumen)
    if descartes:
        with open(ab.SALIDA / "Descartados por riesgo de rebote.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(descartes[0])); w.writeheader(); w.writerows(descartes)
        print(f"Filtro anti-rebote: {len(descartes)} decisores descartados · "
              + ", ".join(f"{m} {n}" for m, n in collections.Counter(d["Motivo"].split(" (")[0] for d in descartes).most_common()))
    print("Exclusión en Amplemarket (rebotes y dominios spam_blocked):", filtro_rebote.subir_exclusion())
    print(f"Listo. Archivos en {ab.SALIDA}")


if __name__ == "__main__":
    main()
