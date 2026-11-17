"""Cliente de HubSpot por lotes: búsquedas con IN, asociaciones v4 y lecturas batch.

Regla del monorepo: nunca consultar de a un registro. Todo aquí recibe listas.
"""
import time

import requests

API = "https://api.hubapi.com"

PROPS_EMPRESA = [
    "name", "domain", "hubspot_owner_id", "lifecyclestage", "hs_lead_status", "createdate",
    "notes_last_contacted", "hs_last_sales_activity_timestamp", "num_associated_contacts",
    "num_associated_deals", "hs_num_open_deals", "created_or_updated_by_amplemarket",
]
PROPS_CONTACTO = [
    "email", "firstname", "lastname", "jobtitle", "hubspot_owner_id", "lifecyclestage",
    "hs_lead_status", "createdate", "notes_last_contacted", "hs_sales_email_last_replied",
    "hs_sales_email_last_opened", "engagements_last_meeting_booked", "hs_latest_sequence_enrolled",
    "hs_latest_sequence_enrolled_date", "created_or_updated_by_amplemarket", "associatedcompanyid",
]
PROPS_DEAL = [
    "dealname", "dealstage", "pipeline", "amount", "deal_currency_code", "createdate", "closedate",
    "hubspot_owner_id", "hs_is_closed", "hs_is_closed_won",
]
PROPS_LEAD = ["hs_lead_name", "hs_pipeline_stage", "hs_createdate", "hubspot_owner_id"]
# Tipo de actividad → propiedades que leemos
PROPS_ACTIVIDAD = {
    "emails": ["hs_timestamp", "hubspot_owner_id", "hs_email_direction", "hs_email_subject",
               "hs_email_status", "hs_email_to_email", "hs_email_from_email"],
    "calls": ["hs_timestamp", "hubspot_owner_id", "hs_call_direction", "hs_call_disposition",
              "hs_call_duration", "hs_call_title", "hs_call_status"],
    "meetings": ["hs_timestamp", "hubspot_owner_id", "hs_meeting_title", "hs_meeting_outcome",
                 "hs_meeting_start_time", "hs_createdate"],
    "communications": ["hs_timestamp", "hubspot_owner_id", "hs_communication_channel_type",
                       "hs_communication_body"],
    "notes": ["hs_timestamp", "hubspot_owner_id", "hs_note_body"],
}


class HubSpot:
    def __init__(self, token: str):
        self.sesion = requests.Session()
        self.sesion.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    def _pedir(self, metodo: str, ruta: str, cuerpo: dict | None = None) -> dict:
        for intento in range(8):
            r = self.sesion.request(metodo, API + ruta, json=cuerpo, timeout=90)
            if r.status_code in (429, 502, 503, 504):
                time.sleep(1.5 + intento * 2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HubSpot {r.status_code} en {ruta}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise RuntimeError(f"HubSpot no respondió en {ruta} tras varios reintentos")

    def verificar(self) -> None:
        self._pedir("GET", "/crm/v3/objects/companies?limit=1")

    # ── catálogos ───────────────────────────────────────────
    def owners(self) -> list[dict]:
        filas, despues = [], None
        while True:
            ruta = "/crm/v3/owners?limit=500&archived=false" + (f"&after={despues}" if despues else "")
            d = self._pedir("GET", ruta)
            filas += d.get("results", [])
            despues = (d.get("paging") or {}).get("next", {}).get("after")
            if not despues:
                return filas

    def etapas_deal(self) -> dict[str, dict]:
        etapas = {}
        for p in self._pedir("GET", "/crm/v3/pipelines/deals").get("results", []):
            for s in p.get("stages", []):
                etapas[s["id"]] = {"etiqueta": s["label"], "pipeline": p["label"],
                                   "cerrada": s.get("metadata", {}).get("isClosed") == "true"}
        return etapas

    def etapas_lead(self) -> dict[str, str]:
        """{id de etapa: etiqueta} del pipeline del objeto Leads (vacío si el portal no lo usa)."""
        try:
            pipes = self._pedir("GET", "/crm/v3/pipelines/leads").get("results", [])
        except RuntimeError:
            return {}
        return {e["id"]: e["label"] for p in pipes for e in p.get("stages", [])}

    def etiquetas(self, objeto: str, propiedad: str) -> dict[str, str]:
        """{valor interno: etiqueta visible} de una propiedad de selección (p. ej. lifecyclestage)."""
        try:
            d = self._pedir("GET", f"/crm/v3/properties/{objeto}/{propiedad}")
        except RuntimeError:
            return {}
        return {o["value"]: o["label"] for o in d.get("options", [])}

    def portal_id(self) -> int | None:
        try:
            return self._pedir("GET", "/account-info/v3/details").get("portalId")
        except RuntimeError:
            return None

    # ── búsqueda por lotes ──────────────────────────────────
    def buscar_en(self, objeto: str, propiedad: str, valores: list[str], props: list[str]) -> list[dict]:
        """Busca registros cuyo `propiedad` esté en `valores` (IN de a 100, paginado)."""
        filas, valores = [], sorted({v for v in valores if v})
        for i in range(0, len(valores), 100):
            despues = None
            while True:
                cuerpo = {
                    "filterGroups": [{"filters": [{"propertyName": propiedad, "operator": "IN",
                                                   "values": valores[i:i + 100]}]}],
                    "properties": props, "limit": 200,
                }
                if despues:
                    cuerpo["after"] = despues
                d = self._pedir("POST", f"/crm/v3/objects/{objeto}/search", cuerpo)
                filas += d.get("results", [])
                despues = (d.get("paging") or {}).get("next", {}).get("after")
                if not despues:
                    break
        return filas

    def asociaciones(self, desde: str, hacia: str, ids: list[str]) -> dict[str, list[str]]:
        """{id_origen: [ids destino]} usando la API v4 batch (1000 por llamada)."""
        res: dict[str, list[str]] = {}
        ids = sorted(set(ids))
        for i in range(0, len(ids), 1000):
            d = self._pedir("POST", f"/crm/v4/associations/{desde}/{hacia}/batch/read",
                            {"inputs": [{"id": x} for x in ids[i:i + 1000]]})
            for r in d.get("results", []):
                origen = str(r["from"]["id"])
                res.setdefault(origen, []).extend(str(t["toObjectId"]) for t in r.get("to", []))
                # páginas extra de asociaciones (empresas con miles de actividades)
                despues = (r.get("paging") or {}).get("next", {}).get("after")
                while despues:
                    p = self._pedir("GET", f"/crm/v4/objects/{desde}/{origen}/associations/{hacia}"
                                           f"?limit=500&after={despues}")
                    res[origen].extend(str(t["toObjectId"]) for t in p.get("results", []))
                    despues = (p.get("paging") or {}).get("next", {}).get("after")
        return res

    def leer(self, objeto: str, ids: list[str], props: list[str]) -> dict[str, dict]:
        """{id: propiedades} con batch/read de a 100."""
        res, ids = {}, sorted(set(ids))
        for i in range(0, len(ids), 100):
            d = self._pedir("POST", f"/crm/v3/objects/{objeto}/batch/read",
                            {"inputs": [{"id": x} for x in ids[i:i + 100]], "properties": props})
            for r in d.get("results", []):
                res[str(r["id"])] = r.get("properties", {})
        return res
