const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = 'https://nuevo-portal.conservador.cl/';
const OUT = path.resolve(__dirname, '..', 'cbrs-network', 'spa-surface-map.json');
const TMP = path.join(os.tmpdir(), `cbrs-bundle-${Date.now()}`);

function uniq(values) {
  return [...new Set(values)].sort((a, b) => a.localeCompare(b));
}

function relToUrl(value) {
  return new URL(value, ROOT).href;
}

function contextSnippets(text, needle, radius = 450) {
  const out = [];
  let idx = 0;
  while ((idx = text.indexOf(needle, idx)) !== -1) {
    const start = Math.max(0, idx - radius);
    const end = Math.min(text.length, idx + needle.length + radius);
    out.push(text.slice(start, end).replace(/\s+/g, ' '));
    idx += needle.length;
    if (out.length >= 6) break;
  }
  return out;
}

function classifyEndpoint(endpoint) {
  if (endpoint.includes('/auth/')) return 'auth';
  if (endpoint.includes('/user/')) return 'user';
  if (endpoint.includes('/home/')) return 'startup';
  if (endpoint.includes('/comercio/')) return 'commerce';
  if (endpoint.includes('/propiedad/')) return 'property';
  if (endpoint.includes('/documentos/')) return 'documents';
  if (endpoint.includes('/consulta-en-linea/')) return 'online-query';
  if (endpoint.includes('/notarioElectronico/')) return 'electronic-notary';
  if (endpoint.includes('/fna/')) return 'fna-verification';
  return 'other';
}

function removeDirQuietly(dir) {
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {}
}

async function fetchText(url) {
  const res = await fetch(url, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome Safari',
      Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    },
  });
  const text = await res.text();
  return { status: res.status, headers: Object.fromEntries(res.headers.entries()), text };
}

async function main() {
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.mkdirSync(TMP, { recursive: true });

  const root = await fetchText(ROOT);
  const html = root.text;
  const seenRefs = new Set();
  const queue = uniq([
    ...html.matchAll(/(?:src|href)="([^"]+\.(?:js|css))"/g),
  ].map((m) => m[1]));

  const assets = [];
  while (queue.length) {
    const ref = queue.shift();
    if (seenRefs.has(ref)) continue;
    seenRefs.add(ref);
    const url = relToUrl(ref);
    const fetched = await fetchText(url);
    const fileName = path.basename(new URL(url).pathname);
    const localPath = path.join(TMP, fileName);
    fs.writeFileSync(localPath, fetched.text);
    assets.push({ ref, url, status: fetched.status, bytes: Buffer.byteLength(fetched.text), fileName, text: fetched.text });
    for (const match of fetched.text.matchAll(/["'`]((?:\/assets|\/chunks)\/[^"'`\\]+\.(?:js|css))["'`]/g)) {
      if (!seenRefs.has(match[1])) queue.push(match[1]);
    }
    for (const match of fetched.text.matchAll(/["'`]((?:\.\/|\.\.\/)[^"'`\\]+\.(?:js|css))["'`]/g)) {
      const base = new URL(url);
      const resolved = new URL(match[1], base).pathname;
      if (!seenRefs.has(resolved)) queue.push(resolved);
    }
  }

  const combined = assets.map((a) => `\n/* ${a.fileName} */\n${a.text}`).join('\n');
  const apiEndpoints = uniq([
    ...combined.matchAll(/["'`]((?:\/api)?\/v1\/[^"'`\\\s<>{}()]+)["'`]/g),
    ...combined.matchAll(/((?:\/api)?\/v1\/[A-Za-z0-9_./${}-]+)/g),
  ].map((m) => m[1].replace(/\$\{[^}]+\}/g, '{param}')));

  const routeCandidates = uniq([
    ...combined.matchAll(/["'`](\/(?:consultas-en-linea|tramites-en-linea|informacion|usuario|carro|pago|verificacion|notario|registro|documentos)[^"'`\\]*)["'`]/g),
  ].map((m) => m[1]));

  const externalHosts = uniq([
    ...combined.matchAll(/https?:\/\/[A-Za-z0-9_.:-]+/g),
  ].map((m) => m[0]));

  const endpointDetails = apiEndpoints.map((endpoint) => ({
    endpoint,
    category: classifyEndpoint(endpoint),
  }));

  const routeDetails = routeCandidates.map((route) => ({
    route,
  }));

  const out = {
    capturedAt: new Date().toISOString(),
    root: {
      url: ROOT,
      status: root.status,
      contentSecurityPolicyMeta: html.match(/Content-Security-Policy" content="([^"]+)"/)?.[1] || null,
      hasIncapsulaResource: html.includes('/_Incapsula_Resource'),
    },
    assets: assets.map(({ text, ...safe }) => safe),
    endpointCount: apiEndpoints.length,
    endpointsByCategory: endpointDetails.reduce((acc, item) => {
      acc[item.category] ||= [];
      acc[item.category].push(item.endpoint);
      return acc;
    }, {}),
    endpointDetails,
    routeCount: routeCandidates.length,
    routeDetails,
    externalHosts,
    note: 'Raw downloaded bundles were used only in a temporary directory for extraction and were removed after writing this summary.',
  };

  fs.writeFileSync(OUT, JSON.stringify(out, null, 2));
  removeDirQuietly(TMP);
  console.log(JSON.stringify({
    out: OUT,
    assets: out.assets.length,
    endpointCount: out.endpointCount,
    routeCount: out.routeCount,
    categories: Object.fromEntries(Object.entries(out.endpointsByCategory).map(([k, v]) => [k, v.length])),
  }, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
