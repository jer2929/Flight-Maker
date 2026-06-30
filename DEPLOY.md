# Deploying Minima to your own domain (Fly.io + Cloudflare)

This guide takes Minima from the repo to **https://personalminimums.com**, running
as an installable PWA, for roughly **$1–3/month**.

```
personalminimums.com  →  Cloudflare (registrar + DNS + TLS/CDN)
                              │   (DNS record; cache bypassed for /api/*)
                              ▼
                          Fly.io  →  Docker container running `uvicorn app.main:app`
                                     (auto-stops when idle; wakes in ~1-2s)
```

**Why this split:** Minima is a FastAPI *backend* (it fetches live NAV CANADA /
Open-Meteo data server-side), so it can't live on static-only hosting like
Cloudflare Pages or GitHub Pages. Fly runs the container; Cloudflare is your
registrar, DNS, and edge.

The repo is already deploy-ready: `Dockerfile`, `fly.toml`, the PWA layer
(`web/manifest.webmanifest`, `web/sw.js`, icons), and a GitHub Action for
auto-deploy are all committed.

---

## One-time setup

### 1. Install flyctl and sign in

```bash
# macOS / Linux
curl -L https://fly.io/install.sh | sh
fly auth signup    # or: fly auth login   (adds a card; usage-billed)
```

### 2. Launch the app (first deploy)

From the repo root:

```bash
fly launch --copy-config --no-deploy
```

- It reads the existing `fly.toml`. Accept or change the app **name** (must be
  globally unique on Fly) and keep the region (`yyz` = Toronto).
- It will **not** add a database (this app doesn't need one — caches are in-memory).

Then deploy:

```bash
fly deploy
```

When it finishes, check the temporary Fly URL works:

```bash
fly open          # opens https://<your-app>.fly.dev
```

You should see Minima. (First load may take ~1-2s while the machine starts.)

### 3. Point your domain at it

**a. Get Fly's IP addresses:**

```bash
fly ips list
```

Fly gives you a free **shared IPv4** and a **dedicated IPv6** by default. Note
both. (A dedicated IPv4 is optional and costs ~$2/mo — not needed here.)

**b. Tell Fly about the domain** so it issues a TLS certificate:

```bash
fly certs add personalminimums.com
fly certs add www.personalminimums.com    # optional
```

**c. Add DNS records in Cloudflare** (Dashboard → your domain → DNS → Records):

| Type  | Name | Content                       | Proxy status            |
|-------|------|-------------------------------|-------------------------|
| A     | `@`  | *(Fly shared IPv4 from step a)* | **DNS only** (grey cloud) |
| AAAA  | `@`  | *(Fly IPv6 from step a)*        | **DNS only** (grey cloud) |

> Keep the proxy **grey (DNS only)** for now — Fly needs an unproxied record to
> validate and issue its Let's Encrypt cert. You can turn the orange proxy on
> later (see "Optional: Cloudflare proxy" below).

**d. Wait for the cert**, then verify:

```bash
fly certs check personalminimums.com
```

Once it reports the certificate is issued (usually a few minutes), visit
**https://personalminimums.com** — you're live, with HTTPS, and the PWA is
installable (look for the install icon in the browser address bar, or "Add to
Home Screen" on mobile).

---

## Continuous deploy (so merging = shipping)

The workflow at `.github/workflows/fly-deploy.yml` deploys on every push to
`main`. It needs one secret.

1. Create a deploy token:

   ```bash
   fly tokens create deploy -x 999999h
   ```

2. In GitHub: **Settings → Secrets and variables → Actions → New repository
   secret**
   - Name: `FLY_API_TOKEN`
   - Value: the token from step 1 (the whole `FlyV1 ...` string)

From then on your workflow is: ask Claude for a change → review/merge the PR →
GitHub Action auto-deploys to Fly → live in ~1-2 minutes. Roll back any time
with `fly releases` + `fly deploy --image <previous>` (or just revert the commit).

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
