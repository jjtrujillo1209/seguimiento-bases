"""Pruebas del filtro anti-rebote: .venv/bin/python -m unittest test_filtro_rebote"""
import collections
import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import filtro_rebote as fr

MX_OK = {"estado": "con_mx", "mx": ["aspmx.l.google.com"]}
BIEN = {"result": "deliverable", "catch_all": False, "fecha": "2026-11-17"}


def filtro(segunda=None, revalidar_lote=True, **kw):
    datos = dict(rebotados=set(), por_dominio={}, suprimidos_baja=set(), hubspot={},
                 mx={"acme.com": MX_OK, "otra.com": MX_OK}) | kw
    if revalidar_lote and segunda is None:
        segunda = {"ana@acme.com": BIEN}
    return fr.Filtro([], revalidar_lote=revalidar_lote, segunda=segunda if revalidar_lote else None, **datos)


class Reglas(unittest.TestCase):
    def test_pasa_solo_con_todo_en_orden(self):
        self.assertEqual(filtro().evaluar("Ana@Acme.com ", "deliverable"), (True, fr.MARCA_OK))

    def test_risky_y_sin_validar_no_pasan(self):
        for estado in ("risky", "unknown", "", "undeliverable"):
            self.assertFalse(filtro().evaluar("ana@acme.com", estado)[0], estado)

    def test_sin_estado_en_el_csv_decide_la_revalidacion(self):
        self.assertTrue(filtro().evaluar("ana@acme.com", None)[0])

    def test_catch_all_no_pasa_aunque_sea_deliverable(self):
        f = filtro(segunda={"ana@acme.com": BIEN | {"catch_all": True}})
        self.assertEqual(f.evaluar("ana@acme.com", "deliverable")[1], "dominio catch-all (acepta todo y rebota después)")

    def test_sin_revalidacion_no_pasa_nada(self):
        ok, motivo = filtro(revalidar_lote=False).evaluar("ana@acme.com", "deliverable")
        self.assertEqual((ok, motivo), (False, "pendiente de revalidación (sin ella no se importa)"))
        self.assertFalse(filtro(segunda={}).evaluar("ana@acme.com", "deliverable")[0])

    def test_suprimidos(self):
        self.assertIn("rebotó", filtro(rebotados={"ana@acme.com"}).evaluar("ana@acme.com", "deliverable")[1])
        self.assertIn("baja", filtro(suprimidos_baja={"ana@acme.com"}).evaluar("ana@acme.com", "deliverable")[1])
        hs = {"ana@acme.com": "HubSpot: hs_email_bad_address"}
        self.assertEqual(filtro(hubspot=hs).evaluar("ana@acme.com", "deliverable")[1], "HubSpot: hs_email_bad_address")

    def test_un_rebote_en_el_dominio_basta(self):
        dom = {"acme.com": collections.Counter(hard_bounced=1)}
        self.assertFalse(filtro(por_dominio=dom).evaluar("ana@acme.com", "deliverable")[0])
        dom = {"acme.com": collections.Counter(spam_blocked=1)}
        self.assertIn("spam_blocked", filtro(por_dominio=dom).evaluar("ana@acme.com", "deliverable")[1])

    def test_mx_falla_cerrado(self):
        for mx in ({}, {"acme.com": {"estado": "sin_consultar", "mx": None}},
                   {"acme.com": {"estado": "sin_mx", "mx": []}}, {"acme.com": {"estado": "no_existe", "mx": []}}):
            self.assertFalse(filtro(mx=mx).evaluar("ana@acme.com", "deliverable")[0], mx)

    def test_sintaxis(self):
        self.assertEqual(filtro().evaluar("ana..p@acme.com", "deliverable")[1], "sintaxis inválida")


class Pagina:
    def __init__(self, cuerpo, status=200):
        self.cuerpo, self.status_code = cuerpo, status

    def json(self):
        return self.cuerpo

    def raise_for_status(self):
        pass


class SesionFalsa:
    """Simula /email-validations: un lote con resultados en dos páginas."""

    def __init__(self, resultados):
        self.resultados, self.posts, self.gets = resultados, [], []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json["emails"])
        return Pagina({"id": f"L{len(self.posts)}", "status": "queued", "results": []}, 202)

    def get(self, url, timeout=None):
        self.gets.append(url)
        if "page[after]" in url:
            return Pagina({"status": "completed", "results": self.resultados[1:], "_links": {}})
        return Pagina({"status": "completed", "results": self.resultados[:1],
                       "_links": {"next": {"href": "/email-validations/L1?page[after]=x"}}})


class Revalidacion(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.res = [{"email": "a@acme.com", "result": "deliverable", "catch_all": False},
                    {"email": "b@acme.com", "result": "risky", "catch_all": True}]

    def test_lee_todas_las_paginas_y_no_paga_dos_veces(self):
        s = SesionFalsa(self.res)
        r = fr.revalidar(["A@acme.com", "b@acme.com"], sesion=s, carpeta=self.dir, hoy=date(2026, 11, 17), espera=0)
        self.assertEqual(set(r), {"a@acme.com", "b@acme.com"})
        self.assertTrue(r["b@acme.com"]["catch_all"])
        fr.revalidar(["a@acme.com"], sesion=s, carpeta=self.dir, hoy=date(2026, 11, 20), espera=0)
        self.assertEqual(len(s.posts), 1)

    def test_resultado_vencido_se_vuelve_a_validar(self):
        s = SesionFalsa(self.res)
        fr.revalidar(["a@acme.com"], sesion=s, carpeta=self.dir, hoy=date(2026, 9, 1), espera=0)
        r = fr.revalidar(["a@acme.com"], sesion=s, carpeta=self.dir, hoy=date(2026, 11, 17), espera=0)
        self.assertEqual(len(s.posts), 2)
        self.assertEqual(r["a@acme.com"]["fecha"], "2026-11-17")

    def test_lote_pendiente_se_retoma_sin_pagar_otra_vez(self):
        (self.dir / "lote_L9.json").write_text(json.dumps({"id": "L9", "emails": ["a@acme.com", "b@acme.com"]}))
        s = SesionFalsa(self.res)
        r = fr.revalidar(["a@acme.com", "b@acme.com"], sesion=s, carpeta=self.dir, hoy=date(2026, 11, 17), espera=0)
        self.assertEqual((len(s.posts), set(r)), (0, {"a@acme.com", "b@acme.com"}))


class ZBFalsa:
    """Simula ZeroBounce: getcredits + validatebatch."""

    def __init__(self, creditos, estados):
        self.creditos, self.estados, self.lotes = creditos, estados, []

    def get(self, url, params=None, timeout=None):
        return Pagina({"Credits": str(self.creditos)})

    def post(self, url, json=None, timeout=None):
        self.lotes.append([x["email_address"] for x in json["email_batch"]])
        return Pagina({"email_batch": [{"address": x["email_address"], "status": self.estados[x["email_address"]],
                                        "sub_status": ""} for x in json["email_batch"]], "errors": []})


class ZeroBounce(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.estados = {"a@acme.com": "valid", "b@acme.com": "catch-all", "c@acme.com": "invalid"}

    def test_mapea_y_no_paga_dos_veces(self):
        s = ZBFalsa(1000, self.estados)
        kw = dict(carpeta=self.dir, hoy=date(2026, 11, 17), proveedor="zerobounce")
        orig, fr.requests.Session = fr.requests.Session, lambda: s
        try:
            r = fr.revalidar(list(self.estados), **kw)
            fr.revalidar(["a@acme.com"], **kw)
        finally:
            fr.requests.Session = orig
        self.assertEqual(len(s.lotes), 1)
        self.assertEqual((r["a@acme.com"]["result"], r["a@acme.com"]["catch_all"]), ("deliverable", False))
        self.assertTrue(r["b@acme.com"]["catch_all"])
        self.assertEqual(r["c@acme.com"]["result"], "undeliverable")
        f = filtro(segunda=r, mx={"acme.com": MX_OK})
        self.assertEqual([f.evaluar(e, None)[0] for e in sorted(self.estados)], [True, False, False])

    def test_si_bulkapi_da_403_valida_uno_por_uno(self):
        class Sin403(ZBFalsa):
            def post(self, url, json=None, timeout=None):
                return Pagina({}, 403)

            def get(self, url, params=None, timeout=None):
                if "validate" in url:
                    self.lotes.append(params["email"])
                    return Pagina({"address": params["email"], "status": self.estados[params["email"]]})
                return super().get(url, params, timeout)
        s, guardados = Sin403(10, self.estados), []
        fr._zerobounce(sorted(self.estados), guardados.extend, sesion=s, clave="k")
        self.assertEqual([g["result"] for g in guardados], ["deliverable", "risky", "undeliverable"])

    def test_sin_saldo_o_llave_invalida_no_cobra(self):
        for creditos in (2, -1):
            s = ZBFalsa(creditos, self.estados)
            with self.assertRaises(RuntimeError):
                fr._zerobounce(sorted(self.estados), lambda x: None, sesion=s, clave="k")
            self.assertEqual(s.lotes, [])


class VerificarCSV(unittest.TestCase):
    def test_separa_aptas_descartadas_y_duplicados(self):
        d = Path(tempfile.mkdtemp())
        ruta = d / "base.csv"
        with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Nombre", "Email", "Estado email"])
            w.writerows([["Ana", "ana@acme.com", "deliverable"], ["Ana bis", "ANA@acme.com", "deliverable"],
                         ["Beto", "beto@acme.com", "risky"]])
        r = fr.verificar_csv(ruta, filtro=filtro())
        self.assertEqual((r["aptas"], r["descartadas"]), (1, 2))
        with open(r["salidas"]["descartados"], encoding="utf-8-sig") as f:
            motivos = [x["Motivo descarte"] for x in csv.DictReader(f)]
        self.assertIn("duplicado en el mismo archivo", motivos)


class Cohortes(unittest.TestCase):
    def test_mide_el_rebote_por_como_se_filtro(self):
        d = Path(tempfile.mkdtemp())
        with open(d / "S48.Ana.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Email", "Filtro rebote"])
            w.writerows([["a@acme.com", fr.MARCA_OK], ["b@acme.com", "deliverable · sin historial"], ["c@acme.com", ""]])
        fotos = [("a@acme.com", "in_sequence", True), ("b@acme.com", "hard_bounced", True),
                 ("c@acme.com", "completed", True), ("z@otra.com", "hard_bounced", False)]
        c = fr.cohortes(fotos=fotos, archivos=[str(d / "S*.csv")])
        self.assertEqual(c["filtro completo"]["enviado"], 1)
        self.assertEqual(c["filtro completo"]["rebote"], 0)
        self.assertEqual(c["filtro sin revalidar"]["rebote"], 1)
        self.assertEqual(c["sin filtro"]["rebote"] + c["sin filtro"]["enviado"], 2)


if __name__ == "__main__":
    unittest.main()
