// Rasterise web/icon.svg into the PWA PNG icon set using headless Chromium.
//
// The icon is an SVG (gradients + real "Minima" text), so it can't be drawn by
// a pixel rasteriser — we render it in Chromium and screenshot at each size.
//
// Run:  NODE_PATH=$(npm root -g) node scripts/make_icons.cjs
// (Playwright + Chromium are provided by the environment; PLAYWRIGHT_BROWSERS_PATH
//  points at the pre-installed browser.)
const { chromium } = require("playwright");
const { readFileSync } = require("fs");
const path = require("path");

const WEB = path.resolve(__dirname, "..", "web");
const svg = readFileSync(path.join(WEB, "icon.svg"), "utf8");

// "bare" = transparent corners (round icon). "pad" = full-bleed dark square with
// the gauge scaled into the safe zone (for maskable + iOS home-screen icons).
const DARK = "#0a0d11";
const targets = [
  { name: "icon-512.png", size: 512, mode: "bare" },
  { name: "icon-192.png", size: 192, mode: "bare" },
  { name: "favicon-32.png", size: 32, mode: "bare" },
  { name: "apple-touch-icon.png", size: 180, mode: "pad", scale: 0.94 },
  { name: "icon-maskable-512.png", size: 512, mode: "pad", scale: 0.80 },
];

function page(size, mode, scale) {
  const inner = Math.round(size * (scale || 1));
  const bg = mode === "pad" ? DARK : "transparent";
  return `<!doctype html><meta charset="utf8"><style>
    html,body{margin:0;padding:0}
    body{width:${size}px;height:${size}px;background:${bg};
         display:flex;align-items:center;justify-content:center;overflow:hidden}
    svg{width:${inner}px;height:${inner}px;display:block}
  </style>${svg}`;
}

(async () => {
  const browser = await chromium.launch();
  for (const t of targets) {
    const p = await browser.newPage({ viewport: { width: t.size, height: t.size }, deviceScaleFactor: 1 });
    await p.setContent(page(t.size, t.mode, t.scale), { waitUntil: "load" });
    await p.screenshot({ path: path.join(WEB, t.name), omitBackground: t.mode === "bare" });
    await p.close();
    console.log("wrote", t.name);
  }
  await browser.close();
})();
