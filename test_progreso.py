"""Progreso por etapa a partir de las tareas de Amplemarket. Correr: .venv/bin/python -m unittest test_progreso"""
import unittest

from amplemarket import progreso_por_etapa


def tarea(n, contacto, tipo, estado, usuario="a@platzi.com", automatica=True):
    return {"id": f"01a1-{n:04d}", "type": tipo, "status": estado, "automatic": automatica,
            "user_email": usuario, "contact": {"id": contacto}}


class ProgresoPorEtapa(unittest.TestCase):
    def test_cuenta_por_etapa_estado_y_usuario(self):
        etapas = progreso_por_etapa([
            tarea(1, "c1", "linkedin_visit", "completed"),
            tarea(2, "c2", "linkedin_visit", "skipped", usuario="B@platzi.com"),
            tarea(3, "c1", "linkedin_like_last_post", "scheduled"),
            tarea(4, "c2", "linkedin_like_last_post", "cancelled", usuario="B@platzi.com"),
        ])
        self.assertEqual([e["tipo"] for e in etapas], ["linkedin_visit", "linkedin_like_last_post"])
        visita, like = etapas
        self.assertEqual((visita["completadas"], visita["saltadas"], visita["programadas"]), (1, 1, 0))
        self.assertEqual((like["programadas"], like["canceladas"]), (1, 1))
        self.assertEqual(visita["por_usuario"]["b@platzi.com"]["saltadas"], 1)
        self.assertTrue(visita["automatica"])

    def test_un_tipo_repetido_se_separa_por_ocurrencia_del_contacto(self):
        etapas = progreso_por_etapa([
            tarea(1, "c1", "phone_call", "completed", automatica=False),
            tarea(2, "c1", "email", "completed", automatica=False),
            tarea(3, "c1", "phone_call", "scheduled", automatica=False),
            tarea(4, "c2", "phone_call", "skipped", automatica=False),
        ])
        self.assertEqual([(e["tipo"], e["n"]) for e in etapas], [("phone_call", 1), ("email", 1), ("phone_call", 2)])
        self.assertEqual((etapas[0]["completadas"], etapas[0]["saltadas"]), (1, 1))
        self.assertEqual(etapas[2]["programadas"], 1)
        self.assertFalse(etapas[0]["automatica"])

    def test_sin_tareas_o_estado_desconocido(self):
        self.assertEqual(progreso_por_etapa([]), [])
        self.assertEqual(progreso_por_etapa([tarea(1, "c1", "email", "otro")]), [])


if __name__ == "__main__":
    unittest.main()
