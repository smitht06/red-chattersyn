// ChatterSyn's Google sign-in relay: a Cloudflare Pages Function serving
// POST https://chattersyn.redcyberdragon.net/api/google/token
//
// The desktop app's Google OAuth client secret must stay out of public source,
// so the app sends its token requests here instead of to Google. This adds the
// app's client ID and secret (GOOGLE_CLIENT_SECRET, a secret on the
// chattersyn-site Pages project) and forwards the request to Google. It only
// relays the two requests the app makes (the sign-in code exchange, for
// loopback redirects only, and token refreshes), and it stores and logs nothing.

const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";
// Public: it's in every sign-in URL. Same value as BUILTIN_CLIENT_ID in
// src/chattersyn/services/youtube/auth.py.
const CLIENT_ID = "449926753228-i28f73kahf4hv37vg3th7oep7fsn7l02.apps.googleusercontent.com";
// The desktop app's redirect (RFC 8252 loopback): http://localhost:<port>
const LOOPBACK = /^http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?\/?$/;

export async function onRequestPost({ request, env }) {
  if (!env.GOOGLE_CLIENT_SECRET) {
    return oauthError(503, "temporarily_unavailable", "sign-in relay is not configured");
  }
  let form;
  try {
    form = await request.formData();
  } catch {
    return oauthError(400, "invalid_request", "expected a form-encoded body");
  }

  const upstream = new URLSearchParams({
    client_id: CLIENT_ID,
    client_secret: env.GOOGLE_CLIENT_SECRET,
  });
  const grant = form.get("grant_type");
  if (grant === "authorization_code") {
    const code = form.get("code");
    const redirect = form.get("redirect_uri") || "";
    if (!code || !LOOPBACK.test(redirect)) {
      return oauthError(400, "invalid_request", "needs a code and a loopback redirect_uri");
    }
    upstream.set("grant_type", grant);
    upstream.set("code", code);
    upstream.set("redirect_uri", redirect);
    const verifier = form.get("code_verifier");
    if (verifier) upstream.set("code_verifier", verifier);
  } else if (grant === "refresh_token") {
    const refresh = form.get("refresh_token");
    if (!refresh) return oauthError(400, "invalid_request", "needs a refresh_token");
    upstream.set("grant_type", grant);
    upstream.set("refresh_token", refresh);
  } else {
    return oauthError(400, "unsupported_grant_type", "only sign-in and refresh are relayed");
  }

  const reply = await fetch(GOOGLE_TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: upstream,
  });
  return new Response(reply.body, {
    status: reply.status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

function oauthError(status, error, description) {
  return Response.json(
    { error, error_description: description },
    { status, headers: { "cache-control": "no-store" } },
  );
}
