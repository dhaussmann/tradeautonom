/**
 * Cloudflare Worker — Reverse proxy for Variational API.
 * Rewrites requests from var-proxy.defitool.de → omni.variational.io
 * Spoofs full browser headers to bypass Cloudflare Managed Challenge.
 */

const UPSTREAM = "https://omni.variational.io";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const upstream = new URL(url.pathname + url.search, UPSTREAM);

    // Build clean headers that look like a real Chrome browser
    const headers = new Headers();
    headers.set("Host", "omni.variational.io");
    headers.set("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36");
    headers.set("Accept", "application/json, text/plain, */*");
    headers.set("Accept-Language", "en-US,en;q=0.9");
    headers.set("Accept-Encoding", "gzip, deflate, br");
    headers.set("Connection", "keep-alive");
    headers.set("Sec-Ch-Ua", '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"');
    headers.set("Sec-Ch-Ua-Mobile", "?0");
    headers.set("Sec-Ch-Ua-Platform", '"macOS"');
    headers.set("Sec-Fetch-Dest", "empty");
    headers.set("Sec-Fetch-Mode", "cors");
    headers.set("Sec-Fetch-Site", "same-origin");
    headers.set("Origin", "https://omni.variational.io");
    headers.set("Referer", "https://omni.variational.io/");

    // Forward auth-relevant headers from the original request
    const cookie = request.headers.get("Cookie");
    if (cookie) headers.set("Cookie", cookie);
    const vrAddr = request.headers.get("vr-connected-address");
    if (vrAddr) headers.set("vr-connected-address", vrAddr);
    const ct = request.headers.get("Content-Type");
    if (ct) headers.set("Content-Type", ct);

    const init = {
      method: request.method,
      headers,
      body: request.method !== "GET" && request.method !== "HEAD"
        ? request.body
        : undefined,
      redirect: "follow",
    };

    console.log(`[proxy] ${request.method} ${url.pathname} → ${upstream.toString()}`);

    const resp = await fetch(upstream.toString(), init);

    console.log(`[proxy] ${request.method} ${url.pathname} ← ${resp.status}`);

    const respHeaders = new Headers(resp.headers);
    respHeaders.set("Access-Control-Allow-Origin", "*");
    respHeaders.set("X-Proxy", "var-proxy-worker");

    return new Response(resp.body, {
      status: resp.status,
      statusText: resp.statusText,
      headers: respHeaders,
    });
  },
};
