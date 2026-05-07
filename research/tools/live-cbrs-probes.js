const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const PROFILE = process.env.CBRS_PROFILE_COPY;
const OUT = path.resolve(__dirname, '..', 'cbrs-network', 'live-probe-summary.json');
const SITE = 'https://nuevo-portal.conservador.cl';
const SITE_KEY = '6Le-eiksAAAAANU-0ITcjxvGfFoHsz40juvUVI_-';

if (!PROFILE) {
  console.error('Set CBRS_PROFILE_COPY to a temporary copied Chromium profile path.');
  process.exit(2);
}

function safeDataShape(data) {
  if (Array.isArray(data)) {
    return {
      type: 'array',
      count: data.length,
      firstKeys: data[0] && typeof data[0] === 'object' ? Object.keys(data[0]).filter((k) => !/ticket|token|email|rut|nombre|direccion|celular/i.test(k)).slice(0, 20) : [],
      firstPublicFields: data[0] && typeof data[0] === 'object'
        ? Object.fromEntries(Object.entries(data[0]).filter(([k, v]) => /^(id|foja|num|numero|ano|acto|tipo|folio|esVisible|isFna|isTomo|statusTomo|numberOfPages|pageNumber)$/i.test(k) && ['string', 'number', 'boolean'].includes(typeof v)))
        : null,
    };
  }
  if (data && typeof data === 'object') {
    return {
      type: 'object',
      keys: Object.keys(data).filter((k) => !/ticket|token|email|rut|nombre|direccion|celular/i.test(k)).slice(0, 40),
      publicFields: Object.fromEntries(Object.entries(data).filter(([k, v]) => /^(status|ok|code|msg|message|numberOfPages|isFna|isTomo|statusTomo|anoDesdeComercio|anoDesdePropiedad|indexMaxRows|indexMaxRows2)$/i.test(k) && ['string', 'number', 'boolean'].includes(typeof v))),
    };
  }
  return { type: typeof data };
}

async function pageFetch(page, url, options) {
  return await page.evaluate(async ({ url, options }) => {
    const res = await fetch(url, options);
    const contentType = res.headers.get('content-type') || '';
    let data = null;
    if (contentType.includes('application/json')) {
      data = await res.json().catch(() => null);
    } else {
      const buf = await res.arrayBuffer().catch(() => null);
      data = buf ? { byteLength: buf.byteLength, contentType } : null;
    }
    return {
      status: res.status,
      contentType,
      cacheStatus: res.headers.get('x-cache-status'),
      data,
    };
  }, { url, options });
}

async function recaptcha(page, action) {
  return await page.evaluate(async ({ SITE_KEY, action }) => {
    if (!window.grecaptcha?.enterprise) return null;
    await new Promise((resolve) => window.grecaptcha.enterprise.ready(resolve));
    return await window.grecaptcha.enterprise.execute(SITE_KEY, { action });
  }, { SITE_KEY, action });
}

async function main() {
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  const context = await chromium.launchPersistentContext(PROFILE, {
    headless: true,
    viewport: { width: 1365, height: 900 },
  });
  const page = await context.newPage();
  const summary = {
    capturedAt: new Date().toISOString(),
    profilePath: '[TEMP_PROFILE_COPY]',
    loggedIn: false,
    routes: [],
    probes: [],
    notes: [],
  };

  async function routeProbe(route) {
    const response = await page.goto(SITE + route, { waitUntil: 'domcontentloaded', timeout: 15000 }).catch((error) => ({ error }));
    await page.waitForTimeout(1000);
    const info = await page.evaluate(() => ({
      url: location.href,
      title: document.title,
      textSample: document.body.innerText.slice(0, 800).replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, '[EMAIL_REDACTED]'),
      hasAuthStorage: Boolean(localStorage.getItem('auth_cbrs_token')),
      hasRecaptcha: Boolean(window.grecaptcha?.enterprise),
    }));
    summary.routes.push({
      route,
      status: response && response.status ? response.status() : null,
      error: response && response.error ? response.error.message : null,
      url: info.url,
      title: info.title,
      hasAuthStorage: info.hasAuthStorage,
      hasRecaptcha: info.hasRecaptcha,
      textSignals: {
        loginRequired: /iniciar sesi[oó]n|ingresar|login/i.test(info.textSample),
        noLoad: /no se pudo cargar/i.test(info.textSample),
        hasIndexText: /Índice|Indice|Consulta|Documento|Foja|Número|Año/i.test(info.textSample),
      },
    });
    return info;
  }

  await routeProbe('/');
  const accountInfo = await routeProbe('/usuario/mi-cuenta');
  summary.loggedIn = accountInfo.hasAuthStorage && !/iniciar sesi[oó]n|login/i.test(accountInfo.textSample);

  const tokenInfo = await page.evaluate(() => {
    const raw = localStorage.getItem('auth_cbrs_token');
    if (!raw) return { present: false };
    try {
      const parsed = JSON.parse(raw);
      const token = parsed.token || parsed.accessToken || raw;
      const payload = JSON.parse(atob(token.split('.')[1]));
      return {
        present: true,
        jwtLike: token.split('.').length === 3,
        expiresAt: payload.exp ? new Date(payload.exp * 1000).toISOString() : null,
        secondsUntilExpiry: payload.exp ? Math.round(payload.exp - Date.now() / 1000) : null,
      };
    } catch {
      return { present: true, jwtLike: false };
    }
  });
  summary.authState = tokenInfo;

  const routeList = [
    '/consultas-en-linea/indices/indice-del-registro-de-comercio',
    '/consultas-en-linea/indices/indice-del-registro-de-propiedad',
    '/verificacion-de-documentos',
    '/verificacion_documentos',
    '/consultas-en-linea',
    '/usuario/mi-cuenta/indice-propiedad-avanzado',
  ];
  for (const route of routeList) await routeProbe(route);

  if (!summary.loggedIn) {
    summary.notes.push('Auth-gated direct probes skipped because the copied profile was not logged in.');
    await context.close();
    fs.writeFileSync(OUT, JSON.stringify(summary, null, 2));
    console.log(JSON.stringify({ out: OUT, loggedIn: false, routeCount: summary.routes.length }, null, 2));
    return;
  }

  await page.goto(SITE + '/consultas-en-linea/indices/indice-del-registro-de-comercio', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await page.waitForTimeout(2000);
  const authHeader = await page.evaluate(() => {
    const raw = localStorage.getItem('auth_cbrs_token');
    const parsed = JSON.parse(raw);
    const token = parsed.token || parsed.accessToken || raw;
    return 'Bearer ' + token;
  });
  const captcha = await recaptcha(page, 'indice_com_texto');
  summary.recaptcha = { commerceAction: 'indice_com_texto', minted: Boolean(captcha), length: captcha ? captcha.length : 0 };

  async function apiProbe(name, url, body, headers = {}, method = 'POST') {
    const result = await pageFetch(page, SITE + url, {
      method,
      headers: { 'content-type': 'application/json', ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    summary.probes.push({
      name,
      url,
      method,
      status: result.status,
      contentType: result.contentType,
      cacheStatus: result.cacheStatus,
      dataShape: safeDataShape(result.data),
    });
    return result;
  }

  await apiProbe('user-me', '/api/v1/user/me', {}, { Authorization: authHeader });
  await apiProbe('home-start', '/api/v1/home/start', { preHint: '' });
  await apiProbe('commerce-no-result', '/api/v1/comercio/indice/texto', {
    foja: null,
    numero: null,
    ano: null,
    texto: 'ZZZNORESULTSCODEX20260506B',
    recaptchaToken: captcha,
    ticket: null,
    titulosAnteriores: false,
    comuna: null,
    anoP: null,
    origen: 'texto',
  }, { Authorization: authHeader, 'recaptcha-token': captcha });
  await apiProbe('commerce-fna-known', '/api/v1/comercio/indice/texto', {
    foja: 63244,
    numero: 27964,
    ano: 2022,
    texto: null,
    recaptchaToken: captcha,
    ticket: null,
    titulosAnteriores: false,
    comuna: null,
    anoP: null,
    origen: 'fna',
  }, { Authorization: authHeader, 'recaptcha-token': captcha });
  await apiProbe('property-base-text', '/api/v1/propiedad/indice/base', { texto: 'MBX Global' }, { Authorization: authHeader });
  await apiProbe('property-text-guess-no-result', '/api/v1/propiedad/indice/texto', {
    foja: null,
    numero: null,
    ano: null,
    texto: 'ZZZNORESULTSCODEX20260506B',
    recaptchaToken: captcha,
    comuna: null,
    origen: 'texto',
  }, { Authorization: authHeader, 'recaptcha-token': captcha });
  await apiProbe('invalid-doc-code-shape', '/api/v1/consulta-en-linea/verifica-doc/validaCodigo', {
    codigo: 'INVALID-CODEX-20260506',
  }, { Authorization: authHeader, 'recaptcha-token': captcha });
  await apiProbe('notario-electronico-base', '/api/v1/notarioElectronico', {});

  await context.close();
  fs.writeFileSync(OUT, JSON.stringify(summary, null, 2));
  console.log(JSON.stringify({ out: OUT, loggedIn: true, routeCount: summary.routes.length, probeCount: summary.probes.length }, null, 2));
}

main().catch(async (error) => {
  console.error(error.message);
  process.exit(1);
});
