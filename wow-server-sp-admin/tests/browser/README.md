# Armory browser check

`armory_check.py` renders the three spike characters (guid 2506 Vegivaca, 4957 Sariel,
7138 Pitokax) with the **real** admin app, the **real** asset proxy and **Wowhead's real
model viewer**, in headless Chromium. It is the only check that talks to the internet, so
it is not a unit test: pytest never collects it, and it is run by hand, after a change to
the viewer integration (`armory.js`, the CSP, the proxy allow-list) or after the pinned
viewer script was updated.

It never touches the live stack: run it only in the throw-away container below (no Docker
socket, no `/opt/stacks`, no published port). The script refuses to start if either
exists. The only outbound traffic is the proxy fetching allow-listed paths from
`wow.zamimg.com` / `www.wowhead.com` (plus the one-time pip, Playwright and vendor-file
downloads); the browser's own traffic goes to a dead proxy, so nothing but the admin
answers it.

## Run

From the repo root (`wow-server-sp-admin/` is mounted read-only):

```bash
docker run -d --name acup-armory-check \
    -v "$(pwd)/wow-server-sp-admin:/src:ro" -w /src python:3.12-slim sleep infinity
docker exec acup-armory-check sh -c 'test ! -S /var/run/docker.sock && test ! -e /opt/stacks && echo isolated'
docker exec acup-armory-check sh -c \
    'pip install -q -r requirements-dev.txt playwright && playwright install --with-deps chromium'
```

The repo ships placeholders for the vendored browser libraries; the check needs the real
files, in a writable copy of the source (never commit them):

```bash
docker exec acup-armory-check sh -c 'rm -rf /tmp/app && cp -a /src /tmp/app && cd /tmp/app/app/static &&
    python -c "import urllib.request as u
for url, name in [(\"https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js\", \"htmx.min.js\"),
                  (\"https://unpkg.com/htmx-ext-sse@2.2.2/sse.js\", \"htmx-sse.js\"),
                  (\"https://unpkg.com/jquery@3.7.1/dist/jquery.min.js\", \"jquery.min.js\")]:
    open(name, \"wb\").write(u.urlopen(url, timeout=30).read())"'
docker exec -w /tmp/app acup-armory-check python tests/browser/armory_check.py
```

Every check prints a `[PASS]`/`[FAIL]`/`[INFO]` line; the exit status is 1 if any failed.
Screenshots (`impl-*.png`) and every number (`armory-check.json`: requests, upstream URLs
with status and size, cache growth, CSP reports, model-panel state changes) land in
`/tmp/fx/out/`. Copy them out with `docker cp acup-armory-check:/tmp/fx/out/. <dir>`, look
at them, and compare `impl-vegivaca-model-final.png` with the spike's render.

Clean up and confirm the live stack is untouched:

```bash
docker rm -f acup-armory-check
docker ps --format '{{.Names}}\t{{.Status}}'   # ac-* and azerothcore-admin still Up
```

## Options

| option | use |
|---|---|
| `--cache fresh` (default) | clears every cached asset and the display-ID map, keeps the pinned viewer script — the cold-cache numbers (≈ 3–4 MB per new race/gender) |
| `--cache keep` | reuses the cache of the previous run (no Wowhead traffic; skips the growth check) |
| `--cache empty` | also deletes the viewer script (fetches it again) |
| `--only chars,extras,mobile,repeat,players` | run some steps only |
| `--guids 2506` | some characters only |
| `--report-only strict` | also sends `Content-Security-Policy-Report-Only: default-src 'self'` and lists what it would block: how the enforced policy's sources were found |
| `--csp "<policy>"` | enforce another policy (experiments) |
| `--no-vendor-check` | run with placeholder vendor files, e.g. to show what fails without jQuery |

## What it checks

* each character page reaches `#armory-model[data-state="rendered"]` within 30 s; the
  canvas is not blank, also in a screenshot taken right at the flip (the "drawn" check
  does not flip early), and the flip comes after the body `.m2` arrived;
* zero CSP violations (listener and console), zero requests to any origin but the admin,
  every upstream URL of an allow-listed shape, no "no 3D" mark and no pending slot (the
  renumbered display IDs resolve through the item-XML fallback), cache growth per new
  race/gender, and 0 upstream fetches on a repeat load with a cold browser cache;
* the sheath toggle destroys the old viewer and its canvas and renders again with one
  canvas; a mouse drag rotates; at 390 px with touch, a drag on the canvas rotates the
  model and does not scroll the page; the canvas fills its container at both widths;
* icons inside hidden tab panels stay `<img>` until their tab opens, then a missing one
  becomes the empty silhouette and an existing one is kept;
* the Players page names are links, and a clipped name's `…` keeps the class colour.
