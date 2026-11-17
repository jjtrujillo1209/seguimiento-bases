"""Pruebas de entregabilidad.py y gestiones.py: .venv/bin/python -m unittest test_entregabilidad"""
import unittest
from datetime import date

import entregabilidad as en
import gestiones

MX_OK = {"estado": "con_mx", "mx": ["aspmx.l.google.com"]}


def lead(email, estado="in_sequence", contacto="2026-11-05T15:00:00Z", validacion="unverified", **am):
    return {"email": email, "nombre": "N", "validacion": validacion,
            "am": {"estado": estado, "primer_contacto": contacto, "respondio": False, "aperturas": 0,
                   "llamada_agendada": False, "motivo": None, "secuencia": "S", "secuencia_id": "s1",
                   "paso": None, "buzon": "rep@platzi.com"} | am}


def foto(leads, generado="2026-11-10T12:00:00+00:00"):
    return {"generado": generado, "portal_id": 1, "secuencias": [], "bases": [{
        "lista_id": "sec:2026-S45:rep@platzi.com", "rep_email": "rep@platzi.com", "rep_nombre": "Rep",
        "semana_clave": "2026-S45", "inicio": "2026-11-02",
        "empresas": [{"clave": "acme.com", "nombre": "Acme", "dominio": "acme.com", "estado": "contactada",
                      "leads": leads, "deals": [], "actividades": [], "alertas": [], "hubspot": None}]}]}


class Sintaxis(unittest.TestCase):
    def test_validos_e_invalidos(self):
        self.assertTrue(en.sintaxis_ok("ana.perez@empresa.com.co"))
        for malo in ("ana@", "ana@empresa", "ana perez@empresa.com", "ana..p@empresa.com", "@empresa.com"):
            self.assertFalse(en.sintaxis_ok(malo), malo)


class Revision(unittest.TestCase):
    def test_sin_verificar_no_es_apto_aunque_tenga_mx(self):
        r = en.revisar_email("ana@acme.com", "unverified", mx=MX_OK)
        self.assertEqual((r["decision"], r["categoria"]), ("revisar", "desconocido"))

    def test_verificado_con_mx_es_apto(self):
        r = en.revisar_email("ana@acme.com", "verified", mx=MX_OK)
        self.assertEqual((r["decision"], r["proveedor"]), ("apto", "google"))

    def test_catch_all_va_aparte(self):
        self.assertEqual(en.revisar_email("ana@acme.com", "catch_all", mx=MX_OK)["categoria"], "catch_all")

    def test_dominio_sin_mx_o_inexistente_se_bloquea(self):
        for estado in ("sin_mx", "no_existe"):
            r = en.revisar_email("ana@acme.com", "verified", mx={"estado": estado, "mx": []})
            self.assertEqual(r["decision"], "bloquear")

    def test_supresion_y_duplicado_bloquean(self):
        sup = {"ana@acme.com": {"motivo": "hard bounce"}}
        self.assertEqual(en.revisar_email("Ana@Acme.com ", "verified", mx=MX_OK, supresion=sup)["categoria"], "suprimido")
        vistos = set()
        self.assertEqual(en.revisar_email("b@acme.com", "verified", mx=MX_OK, vistos=vistos)["decision"], "apto")
        self.assertEqual(en.revisar_email("b@acme.com", "verified", mx=MX_OK, vistos=vistos)["categoria"], "duplicado")

    def test_verificacion_vencida_vuelve_a_desconocido(self):
        self.assertEqual(en.verificacion("verified", "2026-11-01", date(2026, 11, 16)), "valido")
        self.assertEqual(en.verificacion("verified", "2026-09-01", date(2026, 11, 16)), "desconocido")

    def test_revisar_base_usa_el_mx_del_cache(self):
        class Cache:
            def resolver(self, dominios):
                return {d: MX_OK for d in dominios}
        filas = [{"email": "a@acme.com", "email_status": "verified"}, {"email": "malo@", "email_status": "verified"}]
        self.assertEqual([r["decision"] for r in en.revisar_base(filas, cache=Cache())], ["apto", "bloquear"])


class Estados(unittest.TestCase):
    def test_error_transitorio_es_fallo_temporal(self):
        am = {"estado": "error", "motivo": "The recipient's mailbox is full. Retry later"}
        self.assertEqual(en.estado_tecnico(am), "fallo_temporal")
        self.assertTrue(en.clasificar_fallo(am)["reintentar"])

    def test_rebote_duro_es_permanente_y_bloqueo_es_politica(self):
        self.assertEqual(en.clasificar_fallo({"estado": "hard_bounced"})["clase"], "permanente")
        self.assertFalse(en.clasificar_fallo({"estado": "hard_bounced"})["reintentar"])
        self.assertEqual(en.clasificar_fallo({"estado": "spam_blocked"})["clase"], "politica")
        self.assertIsNone(en.clasificar_fallo({"estado": "in_sequence"}))

    def test_proveedor_por_mx(self):
        self.assertEqual(en.proveedor(["acme-com.mail.protection.outlook.com"]), "microsoft")
        self.assertEqual(en.proveedor([]), "sin_mx")
        self.assertEqual(en.proveedor(None), "sin_consultar")


class Supresion(unittest.TestCase):
    def test_un_rebote_sigue_suprimido_aunque_salga_de_la_foto(self):
        vieja = foto([lead("a@acme.com", "hard_bounced"), lead("b@acme.com", "spam_blocked")], "2026-11-05T00:00:00+00:00")
        nueva = foto([lead("c@acme.com")], "2026-11-10T00:00:00+00:00")
        sup = en.construir_supresion([nueva, vieja])
        self.assertEqual(list(sup), ["a@acme.com"])  # el bloqueo por política no suprime la dirección
        self.assertEqual(sup["a@acme.com"]["desde"], "2026-11-05")

    def test_duplicados_entre_secuencias(self):
        d = foto([lead("a@acme.com"), lead("A@acme.com", secuencia="Otra")])
        self.assertEqual(len(en.duplicados(d)), 1)


class Salud(unittest.TestCase):
    def test_tasas_sobre_contactados_y_alerta_de_pausa(self):
        leads = [lead(f"x{i}@acme.com") for i in range(94)] + [lead(f"r{i}@acme.com", "hard_bounced") for i in range(6)]
        leads += [lead(f"d{i}@acme.com", "draft", contacto=None) for i in range(50)]
        s = en.salud(leads)
        self.assertEqual((s["inscritos"], s["contactados"]), (150, 100))
        self.assertEqual(s["tasa_rebote_duro"], 0.06)  # 6/100 contactados, no 6/150 inscritos
        self.assertEqual(s["nivel"], "pausar")

    def test_sin_contactados_no_inventa_tasa_y_poca_muestra_no_alerta(self):
        self.assertIsNone(en.salud([lead("a@acme.com", "draft", contacto=None)])["tasa_rebote_duro"])
        s = en.salud([lead("a@acme.com", "hard_bounced"), lead("b@acme.com")])
        self.assertEqual((s["nivel"], s["alertas"]), ("sin_muestra", []))


class Gestiones(unittest.TestCase):
    def test_estado_tecnico_y_comercial_van_separados(self):
        respondio_y_reboto = lead("a@acme.com", "hard_bounced", respondio=True)
        g = gestiones.construir(foto([respondio_y_reboto, lead("b@acme.com", "draft", contacto=None)]))
        fila = next(f for f in g["filas"] if f["email"] == "a@acme.com")
        self.assertEqual((fila["tecnico"], fila["comercial"]), ("rebote_duro", "respondio"))
        self.assertEqual(fila["id"], "s1:a@acme.com")
        self.assertEqual(fila["cola"], "fallida")
        self.assertEqual(g["embudo"]["contactos"]["inscritos"]["n"], 2)
        self.assertEqual(g["embudo"]["contactos"]["contactados"]["n"], 1)

    def test_lo_que_no_existe_sale_como_sin_datos(self):
        g = gestiones.construir(foto([lead("a@acme.com")]))
        self.assertIsNone(g["embudo"]["contactos"]["respuesta_positiva"]["n"])
        self.assertIsNone(g["costos"]["valor"])
        self.assertTrue(g["embudo"]["contactos"]["respuesta_positiva"]["como_instrumentar"])


if __name__ == "__main__":
    unittest.main()
