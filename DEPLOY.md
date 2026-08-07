# Deploying Minima (Fly.io)

This guide takes Minima from the repo to a live, installable PWA at
**https://minima-wx.fly.dev**, for roughly **$1–3/month** — with an optional
custom domain later.

```
GitHub (push to main)  →  GitHub Action  →  Fly.io
                                            Docker container running `uvicorn app.main:app`
                                            (auto-stops when idle; wakes in ~1-2s)
```

**Why Fly:** Minima is a FastAPI *backend* (it fetches live NAV CANADA /
Open-Meteo data server-side), so it can't live on static-only hosting like
Cloudflare Pages or GitHub Pages. It needs a container that runs code.

The repo is already deploy-ready: `Dockerfile`, `fly.toml`, the PWA layer
(`web/manifest.webmanifest`, `web/sw.js`, icons), and a GitHub Action for
auto-deploy are all committed. **No local tooling is required** — the workflow
creates the Fly app and deploys it for you.

---

## First deploy (browser only, ~5 minutes)

### 1. Create a Fly account

Sign up at **https://fly.io/app/sign-up** and add a card. Billing is
per-second-of-running; see the cost table below.

### 2. Create an API token

Go to **https://fly.io/user/personal_access_tokens** → **Create token**.
Copy the whole string (it starts with `FlyV1 ...`).

> A personal access token is used because the very first deploy has to *create*
> the app, which an app-scoped deploy token can't do. Once the app exists you can
> swap in a narrower token from the app's **Tokens** tab, or via
> `fly tokens create deploy -x 999999h` if you later install flyctl.

### 3. Add it to GitHub

In the repo: **Settings → Secrets and variables → Actions → New repository
secret**

- Name: `FLY_API_TOKEN`
- Value: the token from step 2

### 4. Deploy

Push (or merge) to `main`. The Action at `.github/workflows/fly-deploy.yml`
creates the app if needed and deploys it. Watch it under the repo's **Actions**
tab; it takes ~2-3 minutes for the first build.

When it goes green, open **https://minima-wx.fly.dev**. (First load may take
~1-2s while the machine wakes.) You can re-run a deploy any time from the
Actions tab via **Run workflow**.

From then on the workflow is: ask Claude for a change → merge the PR → live in
~2 minutes. Roll back by reverting the commit.

---

## Optional: custom domain

Skip this until you actually own a domain. The app is fully usable — and the PWA
fully installable, since Fly gives you HTTPS — on `minima-wx.fly.dev`.

When you do have one (e.g. `personalminimums.com` via Cloudflare):

### Point your domain at it

**a. Tell Fly about the domain** so it issues a TLS certificate.

In the Fly dashboard: your app → **Certificates** → **Add a certificate** → enter
`personalminimums.com`. Fly then shows you exactly which DNS records it wants,
plus the validation status. (CLI equivalent: `fly certs add personalminimums.com`.)

**b. Note Fly's IP addresses** from the app's **Overview** page. You get a free
**shared IPv4** and a **dedicated IPv6** by default. (A dedicated IPv4 is optional
and costs ~$2/mo — not needed here.) CLI equivalent: `fly ips list`.

**c. Add DNS records in Cloudflare** (Dashboard → your domain → DNS → Records):

| Type  | Name | Content                       | Proxy status            |
|-------|------|-------------------------------|-------------------------|
| A     | `@`  | *(Fly shared IPv4 from step a)* | **DNS only** (grey cloud) |
| AAAA  | `@`  | *(Fly IPv6 from step a)*        | **DNS only** (grey cloud) |

> Keep the proxy **grey (DNS only)** for now — Fly needs an unproxied record to
> validate and issue its Let's Encrypt cert. You can turn the orange proxy on
> later (see "Optional: Cloudflare proxy" below).

**d. Wait for the cert.** The Certificates page shows it flip to issued, usually
within a few minutes. Then visit **https://personalminimums.com** — live, with
HTTPS, and installable (look for the install icon in the browser address bar, or
"Add to Home Screen" on mobile).

---

## Optional: Cloudflare proxy (CDN + DDoS shield)

Once the cert is issued you can switch the DNS records to **Proxied** (orange
cloud) to get Cloudflare's CDN and protection in front of the static shell.

If you do:

1. **SSL/TLS → Overview →** set mode to **Full (strict)**.
2. **Add a cache rule so live data is never served stale.** Rules → Cache Rules →
   Create:
   - **If** URI Path starts with `/api/`
   - **Then** Cache eligibility: **Bypass cache**

   (The service worker already refuses to cache `/api/*`; this applies the same
   rule at Cloudflare's edge.)

The static shell (`index.html`, `app.js`, `style.css`, icons) caches happily at
the edge; only `/api/*` must bypass.

---

## Network egress

If you ever run this behind an egress allowlist, Minima needs outbound HTTPS to:

- `plan.navcanada.ca` — METAR/TAF/NOTAM/SIGMET/GFA
- `api.open-meteo.com` — HRDPS hourly model
- `geo.weather.gc.ca` / GeoMet — radar tiles & times
- `davidmegginson.github.io` — OurAirports dataset (first-run airport bootstrap)

Fly's default networking is open, so nothing to do there unless you lock it down.

---

## Cost expectations

| Scenario | Rough Fly cost |
|----------|----------------|
| You + a few buddies (bursty use) | **< $1–2/mo** (machine awake only in short bursts) |
| ~100 light users | **~$2–5/mo** (one small VM absorbs it) |
| Pinned always-on 24/7 | ~$4/mo (512 MB) / ~$2/mo (256 MB) |

Billing is per-second-running, so the auto-stop config (`min_machines_running = 0`)
keeps idle cost near zero. The thing that bends before your bill at higher usage
is the free upstreams' rate limits — lengthen cache TTLs via the `FM_` env vars
(see `app/config.py`) if needed.

---

## Local development is unchanged

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload     # http://127.0.0.1:8000
pytest -q
```

The service worker only activates over HTTPS or on `localhost`, so local dev and
the PWA coexist cleanly.

## Regenerating the app icons

The icon is an SVG attitude indicator (`web/icon.svg`) — edit that to change the
look. The PNG sizes (for iOS / Android / maskable) are rasterised from it with
headless Chromium:

```bash
NODE_PATH=$(npm root -g) node scripts/make_icons.cjs
# writes web/icon-{192,512}.png, icon-maskable-512.png, apple-touch-icon.png, favicon-32.png
```
