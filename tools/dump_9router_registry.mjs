// Despeja os registros de provedores do 9Router em JSON enxuto (so dados publicos; sem segredos/OAuth).
import { pathToFileURL } from "node:url";
import path from "node:path";

const root = process.argv[2];
const mod = await import(pathToFileURL(path.join(root, "open-sse", "providers", "registry", "index.js")).href);
const list = mod.default ?? mod.REGISTRY ?? Object.values(mod).find(Array.isArray) ?? [];
const out = [];
for (const p of list) {
  const t = p.transport || {};
  out.push({
    id: p.id,
    name: p.display?.name || p.id,
    category: p.category,
    hasFree: !!p.hasFree,
    noAuth: !!p.noAuth,
    hasOAuth: !!p.hasOAuth,
    authType: p.authType || "",
    apiKeyUrl: p.display?.notice?.apiKeyUrl || "",
    notice: p.display?.notice?.text || "",
    website: p.display?.website || "",
    baseUrl: t.baseUrl || "",
    validateUrl: t.validateUrl || "",
    format: t.format || "openai",
    headers: t.headers ? Object.keys(t.headers) : [],
    hasExecutor: !!t.executor,
    authModes: p.authModes || [],
    hooks: t.auth?.hooks || [],
    // transportes alternativos (ex.: o mesmo provedor tambem fala OpenAI): so formato, URL e se usa gancho especial
    transports: (p.transports || []).map(x => ({ format: x.format || "", baseUrl: x.baseUrl || "", hooks: x.auth?.hooks || [], header: x.auth?.header || "", scheme: x.auth?.scheme || "" })),
    deprecated: !!p.display?.deprecated,
    serviceKinds: p.serviceKinds || [],
    models: (p.models || []).map(m => ({ id: m.id, name: m.name, kind: m.kind || "llm", ctx: m.contextLength || null })),
  });
}
console.log(JSON.stringify(out));
