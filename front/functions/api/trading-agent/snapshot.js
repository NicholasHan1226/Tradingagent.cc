export async function onRequestGet({ env }) {
  const snapshotUrl = env.TRADING_AGENT_SNAPSHOT_UPSTREAM_URL;

  if (!snapshotUrl) {
    return Response.json(
      {
        status: "upstream_not_configured",
        message:
          "TradingAgent snapshot upstream is not connected to Cloudflare yet.",
      },
      {
        status: 503,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  const upstreamHeaders = new Headers();
  upstreamHeaders.set("Accept", "application/json");
  if (env.TRADING_AGENT_SNAPSHOT_API_TOKEN) {
    upstreamHeaders.set(
      "Authorization",
      `Bearer ${env.TRADING_AGENT_SNAPSHOT_API_TOKEN}`,
    );
  }

  let upstream;
  try {
    upstream = await fetch(snapshotUrl, {
      method: "GET",
      headers: upstreamHeaders,
    });
  } catch {
    return Response.json(
      {
        status: "upstream_unavailable",
        message: "TradingAgent snapshot upstream is temporarily unavailable.",
      },
      {
        status: 502,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.set("Cache-Control", "no-store");

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

export async function onRequestOptions() {
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
      "Cache-Control": "no-store",
    },
  });
}
