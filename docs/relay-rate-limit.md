# Rate-limit the Google sign-in relay

ChatterSyn's YouTube sign-in goes through a small relay at
`https://chattersyn.redcyberdragon.net/api/google/token`
(`site/functions/api/google/token.js`). It adds the Google client secret and
forwards two kinds of request to Google. Nobody can get a token from it without
a real user's sign-in, but anyone could still flood it. Cloudflare Pages
Functions have no rate limiting of their own, so the limit is a Cloudflare
**rate-limiting rule** on the `redcyberdragon.net` zone. The free plan includes
one.

The rule: at most **20 requests per 10 seconds from one IP address** to the
relay path; past that, that IP is blocked for 10 seconds. A real sign-in sends
one or two requests, and a token refresh one, so users never hit it.

## Add the rule (Cloudflare dashboard)

1. Sign in at [dash.cloudflare.com](https://dash.cloudflare.com) and open the
   **redcyberdragon.net** zone.
2. Go to **Security → WAF → Rate limiting rules**, and click **Create rule**.
3. **Rule name:** `ChatterSyn sign-in relay`.
4. **If incoming requests match…**: choose **Edit expression** and enter:

   ```
   (http.host in {"chattersyn.redcyberdragon.net" "redcyberdragon.net"} and http.request.uri.path eq "/api/google/token")
   ```

   Both hosts serve the site (and so the relay); the rule must cover both.

5. **With the same characteristics:** IP (the default).
6. **When rate exceeds:** Requests `20`, Period `10 seconds`.
7. **Then take action:** Block. **For duration:** `10 seconds` (the free plan's
   only option).
8. Click **Deploy**.

## The `pages.dev` address

The site is also reachable at `chattersyn-site.pages.dev`, which isn't part of
the `redcyberdragon.net` zone, so no rule there can cover it. The app never uses
that address. If abuse ever shows up there, the relay function can refuse
requests whose `Host` isn't one of the two custom domains.

## Check that it works

From a terminal, send 25 quick requests and look at the status codes:

```sh
for i in $(seq 25); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST \
    https://chattersyn.redcyberdragon.net/api/google/token
done | sort | uniq -c
```

The first 20 or so answer `400` (the relay rejects an empty request); the rest
answer `429` (blocked by the rule). After 10 seconds requests go through again.
A YouTube sign-in in ChatterSyn should still work as before.
