// Fetch one gateway URL, stream it through sha256 and print a JSON result.
// Node (OpenSSL 3, TLS 1.3) is used because the system Python/curl TLS stack here cannot reach several gateways.
// Usage: node verify-gateway.mjs <url>
import { createHash } from "node:crypto";

const url = process.argv[2];
const started = new Date().toISOString();
const t0 = Date.now();
const out = { url, checked_at: started };
try {
  const res = await fetch(url, { redirect: "follow", headers: { "User-Agent": "oracle-nassau-pipeline/0.1 (ipfs verification)" }, signal: AbortSignal.timeout(180000) });
  const h = createHash("sha256");
  let bytes = 0;
  for await (const chunk of res.body) { h.update(chunk); bytes += chunk.length; }
  Object.assign(out, { status: res.status, final_url: res.url, bytes, sha256: h.digest("hex"), content_type: res.headers.get("content-type"), retry_after: res.headers.get("retry-after") });
  if (res.status !== 200 && bytes < 600) out.body_snippet = "(non-200 body, hash omitted)";
} catch (e) {
  out.status = null;
  out.error = `${e.cause?.code ?? e.name}: ${String(e.message).slice(0, 160)}`;
}
out.seconds = Math.round((Date.now() - t0) / 100) / 10;
console.log(JSON.stringify(out));
