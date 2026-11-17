// Portón de follow-up-outbound. Corre en el Edge, antes que cualquier archivo estático
// (incluidos datos.json e historial.json, que traen contactos y actividad de HubSpot).
//
// Contraseña única (ACCESO_CLAVE) → cookie firmada con SESSION_SECRET por 30 días.
// Mismo formato de token que hub-platzi-secuencias: base64url(payload) + "." + base64url(HMAC).
// Si falta configuración, NO se abre la puerta.

export const config = { matcher: '/:path*' };

const COOKIE = 'fuo_sesion';
const DURACION_MS = 30 * 864e5;
const enc = new TextEncoder();
const dec = new TextDecoder();

const seguir = () => new Response(null, { headers: { 'x-middleware-next': '1' } });

const b64u = (bytes) => {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
};
const unb64u = (str) => {
  const s = str.replace(/-/g, '+').replace(/_/g, '/');
  const bin = atob(s + '='.repeat((4 - (s.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
};
const clave = (secreto) =>
  crypto.subtle.importKey('raw', enc.encode(secreto), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);

async function firmar(payload, secreto) {
  const cuerpo = b64u(enc.encode(JSON.stringify(payload)));
  const sig = await crypto.subtle.sign('HMAC', await clave(secreto), enc.encode(cuerpo));
  return cuerpo + '.' + b64u(new Uint8Array(sig));
}

async function verificar(token, secreto) {
  const corte = (token || '').lastIndexOf('.');
  if (corte < 1) return false;
  try {
    const cuerpo = token.slice(0, corte);
    const ok = await crypto.subtle.verify('HMAC', await clave(secreto), unb64u(token.slice(corte + 1)), enc.encode(cuerpo));
    return ok && Date.now() < JSON.parse(dec.decode(unb64u(cuerpo))).exp;
  } catch {
    return false;
  }
}

// Compara la contraseña en tiempo constante: se firma lo enviado y se verifica contra la firma de la real.
async function claveCorrecta(enviada, real, secreto) {
  const k = await clave(secreto);
  const firmaReal = await crypto.subtle.sign('HMAC', k, enc.encode(real));
  return crypto.subtle.verify('HMAC', k, firmaReal, enc.encode(enviada));
}

const leerCookie = (cabecera) => {
  for (const trozo of (cabecera || '').split(';')) {
    const i = trozo.indexOf('=');
    if (i > 0 && trozo.slice(0, i).trim() === COOKIE) return decodeURIComponent(trozo.slice(i + 1).trim());
  }
  return null;
};

const html = (cuerpo, status = 200, extra = {}) =>
  new Response(
    `<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">` +
      `<meta name="robots" content="noindex"><title>Follow up outbound</title>` +
      `<style>body{background:#121413;color:#DCE5E4;font:15px/1.6 system-ui,sans-serif;display:grid;place-items:center;` +
      `min-height:100vh;margin:0;padding:16px}form{width:min(340px,100%);display:grid;gap:12px}` +
      `h1{font-size:1.25rem;margin:0}p{margin:0;color:#9AA5A3}.err{color:#FEB4AB}` +
      `input,button{font:inherit;padding:10px 12px;border-radius:8px;border:1px solid #3A403E;background:#1C1F1E;color:inherit}` +
      `button{background:#98CA3F;color:#121413;border:0;font-weight:600;cursor:pointer}</style>${cuerpo}</html>`,
    { status, headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store', ...extra } }
  );

const formulario = (error = '') =>
  html(
    `<form method="post" action="/login"><h1>Follow up outbound</h1><p>Seguimiento de bases outbound · Platzi Business</p>` +
      (error ? `<p class="err">${error}</p>` : '') +
      `<input type="password" name="clave" placeholder="Contraseña" autocomplete="current-password" autofocus required>` +
      `<button>Entrar</button></form>`,
    error ? 401 : 200
  );

export default async function middleware(req) {
  const url = new URL(req.url);
  const { ACCESO_CLAVE: real, SESSION_SECRET: secreto } = process.env;
  if (!real || !secreto) {
    return html('<div><h1>Falta configurar el acceso</h1><p>Definir ACCESO_CLAVE y SESSION_SECRET en Vercel.</p></div>', 503);
  }

  if (url.pathname === '/login') {
    if (req.method !== 'POST') return formulario();
    const enviada = String((await req.formData()).get('clave') || '');
    if (!(await claveCorrecta(enviada, real, secreto))) return formulario('Contraseña incorrecta.');
    const token = await firmar({ exp: Date.now() + DURACION_MS }, secreto);
    return new Response(null, {
      status: 303,
      headers: {
        location: '/',
        'set-cookie': `${COOKIE}=${encodeURIComponent(token)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${DURACION_MS / 1000}`,
      },
    });
  }

  if (await verificar(leerCookie(req.headers.get('cookie')), secreto)) return seguir();
  if (url.pathname.endsWith('.json')) {
    return new Response('{"error":"sesion_requerida"}', { status: 401, headers: { 'content-type': 'application/json' } });
  }
  return Response.redirect(new URL('/login', url), 302);
}
