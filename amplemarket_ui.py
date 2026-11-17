"""Leads de secuencias de Amplemarket leídos con la sesión del Chrome dedicado (CDP).

La API pública no expone quién está dentro de una secuencia; la API interna de la web sí
(`/api/v1/sequences/{id}/leads`, paginada por cursor). Se llama desde una pestaña de
app.amplemarket.com para que viaje con la sesión del usuario. Ver chrome_amplemarket.sh.
"""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ORIGEN = "https://app.amplemarket.com"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PERFIL = Path(__file__).parent / "datos" / "chrome-perfil-amplemarket"

JS_LEADS = """
async (id) => {
  let leads = [], cursor = null, vueltas = 0;
  do {
    const u = `/api/v1/sequences/${id}/leads` + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : '');
    const r = await fetch(u, {headers: {accept: 'application/json'}, credentials: 'include'});
    if (r.status === 401 || r.status === 403) return {error: 'sesion'};
    if (!r.ok) return {error: `HTTP ${r.status} en ${u}`};
    const d = await r.json();
    // Secuencias lanzadas responden `leads`; los borradores, `sequence_leads` (formato de preparación)
    const pagina = d.leads || d.sequence_leads || [];
    leads = leads.concat(pagina);
    cursor = pagina.length && d.meta?.has_next_page !== false ? d.meta?.cursor : null;
    if (++vueltas > 200) return {error: 'paginación sin fin'};
  } while (cursor);
  return {leads};
}
"""


class SesionAmplemarketError(RuntimeError):
    pass


def _cdp_activo(puerto: str) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{puerto}/json/version", timeout=2)
        return True
    except OSError:
        return False


def asegurar_chrome(puerto: str) -> None:
    """Abre el Chrome dedicado (mismo perfil y flags que chrome_amplemarket.sh) si no está corriendo.
    La sesión de Amplemarket vive en el perfil, así que no hay que volver a iniciar sesión."""
    if _cdp_activo(puerto):
        # En Mac, cerrar la ventana deja Chrome vivo sin pestañas y entonces el CDP rechaza la
        # conexión («Browser context management is not supported»): se abre una pestaña.
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/json/list", timeout=5) as r:
            if not any(t.get("type") == "page" for t in json.load(r)):
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{puerto}/json/new?{ORIGEN}/dashboard/sequences", method="PUT"), timeout=10)
                time.sleep(4)
        return
    PERFIL.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([
        CHROME, f"--remote-debugging-port={puerto}", f"--user-data-dir={PERFIL}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=HighEfficiencyModeAvailable,IntensiveWakeUpThrottling",
        "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling", f"{ORIGEN}/dashboard/sequences",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        time.sleep(1)
        if _cdp_activo(puerto):
            time.sleep(3)  # deja cargar la pestaña de Amplemarket
            return


def leads_de_secuencias(ids: list[str]) -> dict[str, list[dict]]:
    """{secuencia_id: [leads crudos de la API interna]} usando el Chrome dedicado."""
    puerto = os.getenv("AM_CDP_PORT", "9361")
    asegurar_chrome(puerto)
    with sync_playwright() as pw:
        try:
            nav = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{puerto}", timeout=15000)
        except Exception as exc:
            raise SesionAmplemarketError(
                f"No pude conectar con el Chrome dedicado en :{puerto} ({str(exc).splitlines()[0][:160]}). "
                "Cierra ese Chrome (Cmd+Q) y vuelve a sincronizar: la app lo abre sola.") from exc
        contexto = nav.contexts[0] if nav.contexts else nav.new_context()
        pagina = next((p for p in contexto.pages if p.url.startswith(ORIGEN)), None)
        propia = pagina is None
        if propia:
            pagina = contexto.new_page()
            pagina.goto(f"{ORIGEN}/dashboard/sequences", wait_until="domcontentloaded", timeout=60000)
        try:
            pagina.wait_for_load_state("domcontentloaded", timeout=60000)
            if "/login" in pagina.url or "sign_in" in pagina.url:
                raise SesionAmplemarketError("El Chrome dedicado no tiene sesión en Amplemarket: inicia sesión ahí.")
            res = {}
            for sid in ids:
                r = pagina.evaluate(JS_LEADS, sid)
                if r.get("error") == "sesion":
                    raise SesionAmplemarketError("La sesión de Amplemarket en el Chrome dedicado expiró: vuelve a iniciar sesión.")
                if r.get("error"):
                    raise RuntimeError(f"Amplemarket (secuencia {sid}): {r['error']}")
                res[sid] = r["leads"]
            return res
        finally:
            # Nunca se cierra el navegador del usuario; solo la pestaña que abrimos nosotros
            if propia:
                pagina.close()
