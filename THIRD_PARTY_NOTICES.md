# Third-party notices

`eye-in-the-sky` is licensed under Apache-2.0 (see `LICENSE`). It **bundles and derives from**
third-party software and data under their own licences, reproduced or cited below as those licences
require.

---

## 1. Microsoft AirSim — MIT (derived source)

`godseye/mcp/godseye_uav/geo.py` is, by its own docstrings, a **line-by-line port** of AirSim's
`AirLib/include/common/EarthUtils.hpp`:

- `geo.py:3` — "Ported from AirSim AirLib/include/common/EarthUtils.hpp (verified)."
- `geo.py:86` — "Exact port of `EarthUtils::nedToGeodetic` (EarthUtils.hpp:291)."
- `geo.py:108` — "Exact port of `EarthUtils::GeodeticToEcef`."
- `geo.py:123` — "Exact port of `EarthUtils::EcefToNed`."
- `geo.py:141` — "Port of `EarthUtils::GeodeticToNed`."
- `geo.py:39` — `EARTH_RADIUS` constant from `common_utils/Utils.hpp:49`.

The AirSim Python client (`microsoft/airsim`, pinned at `1ca93f6f77e4e8a39b2b241c1fe2764da4d7dd41`)
is also a **runtime dependency** of this project; it is not vendored here — `godseye/scripts/ci.sh`
and `scripts/setup.sh` fetch it at that pin.

```
The MIT License (MIT)

MSR Aerial Informatics and Robotics Platform
MSR Aerial Informatics and Robotics Simulator (AirSim)
Copyright (c) Microsoft Corporation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. God's Eye View — MIT (vendored in full)

The entire `gods-eye-view/` directory is a **separate upstream project**, vendored here so the
command-center UI and the simulation live in one repo. It is **not** original work of this project,
apart from the UAV integration described below.

- Upstream: <https://github.com/bilawalsidhu/gods-eye-view> (v0.1.1)
- Licence: MIT, **Copyright (c) 2026 Bilawal Sidhu** — see `gods-eye-view/LICENSE`, preserved intact.

**What in that tree belongs to this project** (~32 new files, ~9,200 lines): the UAV layer
`src/layers/uav/**`, the UAV UI `src/ui/uav*.js`, the source adapter `src/sources/live/uav*.js`,
`src/app/layers/uav.js`, and `docs/godseye-mcp-server-design.md`. Roughly 70 further lines are hooks
added into ~13 pre-existing upstream files (for example a single registration line in
`src/app/constructCatalog.js`, `src/data/layerState.js:387`, `src/ui/applicationShell.js:333`).
The intelligence console is also this project's: `src/console/**` (53 files, tests included) and
the tracking port `src/app/trackingPort.js`, with integration edits in `src/main.js`, `index.html`,
`src/app/controls.js` and `src/app/tools.js`.
Everything else in `gods-eye-view/` is upstream code under the MIT licence above.

**Excluded from this repo** (regenerable or bulky, not part of the source): `node_modules/`,
`dist/`, `.gev-cache/`, `.gev-logs/`, and `docs/media/` (67 MB of documentation GIFs — fetch from
upstream if you want them).

---

## 3. Bundled data in `gods-eye-view/` — read before making this repo public

The upstream project ships datasets whose licences are **more restrictive than MIT**. They are
documented in `gods-eye-view/DATA_SOURCES.md`; the ones that constrain redistribution:

| Data | Licence | Constraint |
|---|---|---|
| TeleGeography submarine cables | **CC BY-NC-SA 3.0** | **Non-commercial**, and share-alike on adaptations |
| Bhote Koshi event data | **CC BY-NC 4.0** | **Non-commercial** |
| Natural Earth | Public domain | — |
| DataSF neighborhoods | Public domain / open | attribution |

Because this repository is **private**, use here is personal/research and no public redistribution
occurs. **If it is ever made public or used commercially, the NC terms above are violated** — remove
those datasets first.

## 4. Live data services used at runtime

Not redistributed, but their terms bind how the running system may be used (see
`godseye/REAL_DATA_INTEGRATION.md` and `gods-eye-view/DATA_SOURCES.md`):

- **OpenSky Network** — non-commercial research/education only. Cite Schäfer et al., *"Bringing Up
  OpenSky"*, IPSN 2014.
- **adsb.lol**, **OpenStreetMap / Overpass**, **OSRM (FOSSGIS)** — ODbL 1.0, attribution required.
- **Open-Meteo** — CC BY 4.0, adjacent-link attribution required.
- **Re:Earth Terrain / Mapterhorn** — CC BY 4.0; geoid EGM2008 (NGA, public domain).
- **Google Maps Platform**, **Esri World Imagery** — proprietary, own key and terms.

### 4.1 Services the Python side calls directly (map data and direct hydration)

Since runtime theaters (WG v2 Phase A), `godseye/mcp/godseye_uav/geo_http.py` calls these public
services itself: for map data (on in the app, `--geodata on`) and for `--real-data direct`. Nothing
from them is redistributed in this repository; answers are cached under `<store>/geodata-cache/`
on the user's machine (30 days for place lookups, 24 hours for sites). Every request carries the
User-Agent `EyeInTheSky/<version> (+godseye; contact: $GODSEYE_GEO_CONTACT)`, and each service has
its own minimum spacing between requests (`geo_http.MIN_SPACING_S`).

| Service | What the app sends and uses | Data and attribution |
|---|---|---|
| **Photon** (komoot), `photon.komoot.io` | place-search text; place names, points and boxes | OpenStreetMap data, © OpenStreetMap contributors, **ODbL 1.0** |
| **Nominatim** (OpenStreetMap Foundation), `nominatim.openstreetmap.org` | place-search text, only when Photon fails or finds nothing | OpenStreetMap data, **ODbL 1.0**; the service's usage policy asks for at most one request per second, an identifying User-Agent and attribution |
| **Overpass API**, `overpass-api.de` (one retry on the `overpass.kumi.systems` mirror) | a theater's bounding box; mapped strategic sites and named open ground | OpenStreetMap data, © OpenStreetMap contributors, **ODbL 1.0** |
| **Re:Earth Terrain / Mapterhorn**, `terrain.reearth.land` | single points; ellipsoidal ground heights | **CC BY 4.0**; geoid EGM2008 (NGA, public domain) |
| **Open-Meteo**, `api.open-meteo.com` (`/v1/forecast`, `/v1/elevation`) | single points; current weather, and the ground-height fallback (Copernicus DEM) | **CC BY 4.0**, attribution to Open-Meteo.com; the elevation answer derives from the Copernicus DEM |

The attribution the running app shows, verbatim from the code:

- `geocode.ATTRIBUTION`: "Geocoding: Photon by komoot / Nominatim; data © OpenStreetMap
  contributors, ODbL" (every `geo_lookup` answer's `provenance.attribution`).
- `sites.ATTRIBUTION`: "© OpenStreetMap contributors, ODbL" (`geo_sites`, the intel graph's
  `meta.sites.attribution`, the `/intel/overlay` body's `attribution`), with the caveat "Sites are
  mapped OpenStreetMap data (ODbL), not an order of battle."
- The map dock, whenever sites are drawn: "Map data: © OpenStreetMap contributors, ODbL."; the site
  inspector's source line names "OpenStreetMap contributors, ODbL" and Overpass.
- `realdata.ATTRIBUTION["terrain"]`: "Terrain: Re:Earth / Mapterhorn terrain heights (CC BY 4.0);
  geoid EGM2008 (NGA, public domain)".
- `realdata.ATTRIBUTION["weather"]`: "Weather data by Open-Meteo.com (CC BY 4.0)".
- A theater whose ground came from Open-Meteo says "Copernicus DEM via Open-Meteo (EGM2008), used
  as sea level; the geoid difference is not corrected."

Unverified here, and to check before any use beyond personal research: each service's current
usage policy and rate limits (Photon's and Overpass's public instances are shared, best-effort
services; Nominatim's policy is stated above as generally published, not re-read for this build);
whether Open-Meteo's free API terms allow the intended use (its free tier is generally described as
non-commercial); and the attribution the Copernicus DEM licence requires for derived heights. The
app does not display the Re:Earth or Open-Meteo lines in the console today; they travel in the
provenance of the data they produced, and the theater slip and theater inspector name Re:Earth or
Open-Meteo only in the ground-source sentence. Showing the CC BY lines in the console is an open
item.

## 5. EGM96 geoid grid

`godseye/mcp/godseye_uav/data/us_nga_egm96_15.tif` (2.6 MB) is the NGA EGM96 15-minute geoid grid.
Its own GDAL metadata reads *"Derived from work by NGA. Public Domain"*. It ships as package data
because `geo.canonical_altitude()` is the single datum-conversion point and raises rather than
degrading silently when no geoid source is available.

---

## 6. Claude Agent SDK and the Claude Code CLI (the analyst)

The in-app analyst uses the Python package `claude-agent-sdk` (0.2.160, installed by the `app`
extra of `godseye/pyproject.toml`). It is not vendored in this repository. The desktop build
(`scripts/build_desktop.sh`) freezes the package into the app and copies the Claude Code CLI that the
package bundles (`claude_agent_sdk/_bundled/claude`, version 2.1.283) into
`Contents/Helpers/claude`, unmodified and still signed by Anthropic (`--no-cli` leaves it out).

What the installed distribution says (`claude_agent_sdk-0.2.160.dist-info`):

- `licenses/LICENSE` is the MIT License, "Copyright (c) 2025 Anthropic, PBC". Its text is the same
  MIT text reproduced in section 1, with that copyright line.
- `METADATA` declares `License: MIT`, and its "License and terms" section adds that use of the SDK
  "is governed by Anthropic's Commercial Terms of Service"
  (<https://www.anthropic.com/legal/commercial-terms>), including when it powers products and
  services offered to your own customers and end users, except where a component or dependency is
  covered by a different licence stated in that component's own LICENSE file.
- The wheel carries no LICENSE file for the bundled CLI (`_bundled/` holds only the `claude` binary
  and a `.gitignore`). Nothing in the distribution states that the CLI is under the MIT grant; the
  only terms it points to are the Commercial Terms above. Check Anthropic's terms before
  redistributing a desktop build that includes `Contents/Helpers/claude`.

Separately from the licence, Anthropic's Agent SDK documentation asks third-party products not to
offer claude.ai login or its rate limits without prior approval, and to use API-key
authentication instead. This project uses the owner's own Claude login only for local use; see
`godseye/INTEL_CONSOLE.md`, "Analyst sign-in and Anthropic's policy".

The analyst can also be pointed at other model providers (Analyst settings; the catalog is
`godseye/mcp/godseye_uav/llm_providers.py`). Nothing from those providers is bundled: the app sends
requests to the endpoint the user picks, with the user's own key, and that provider's terms govern
that use. Anthropic states that it doesn't support routing Claude Code to non-Claude models through
any gateway; OpenRouter says Claude Code is only guaranteed to work with its Anthropic first-party
provider; and Z.ai limits its GLM Coding Plan to officially supported tools. See "Analyst
providers" in the repository `README.md`.

## 7. pywebview: BSD 3-Clause

The native window (`--window` and the desktop app) uses `pywebview` (6.2.1, `app` extra), frozen
into the desktop app with its Cocoa backend. Its `licenses/LICENSE` is the BSD 3-Clause License,
"Copyright (c) 2014-2017, Roman Sirokov". Redistribution in binary form must reproduce that copyright
notice, the list of conditions and the disclaimer in the documentation or other materials provided
with the distribution; the full text is in
`godseye/.venv/lib/python3.*/site-packages/pywebview-6.2.1.dist-info/licenses/LICENSE` (and at
<https://github.com/r0x0r/pywebview>). The desktop build does not yet copy it into the app.

## 8. PyInstaller: GPL-2.0-or-later with the bootloader exception

`PyInstaller` (6.22.3, `desktop` extra) builds the macOS app; it is a build tool, not a library the
app imports. Its `licenses/COPYING.txt` says:

- PyInstaller is under the GNU General Public License, version 2 or (at your option) any later
  version.
- **Bootloader exception**: the authors give unlimited permission to link or embed the compiled
  bootloader and related files (`bootloader/`, `PyInstaller/loader`) into combinations with other
  programs and to distribute those combinations without restriction from the use of those files.
  The GPL still applies otherwise, for example to modifying those files or distributing them on
  their own.
- Run-time hooks (`PyInstaller/hooks/rthooks`) and the additional run-time modules
  (`PyInstaller/fake-modules`) that end up inside the executable are under the Apache License 2.0.

So the app PyInstaller produces is not placed under the GPL by being built with it.
`pyinstaller-hooks-contrib` (2026.7, a PyInstaller dependency) follows the same split: its hooks
are GPL-2.0-or-later and the run-time hooks it adds to executables are Apache-2.0.

## 9. Fonts (loaded from Google Fonts, not vendored)

`gods-eye-view/index.html` links stylesheets from `fonts.googleapis.com`; no font file is in this
repository or in the desktop app, and the browser fetches them at run time.

- **Atkinson Hyperlegible Next** and **Atkinson Hyperlegible Mono** (Braille Institute): the
  console's faces. SIL Open Font License 1.1.
- Upstream God's Eye View also loads **Inter** and **JetBrains Mono** (SIL Open Font License 1.1)
  and **Material Symbols Outlined** (Apache License 2.0).

Offline, the browser falls back to local fonts.

## 10. Other Python packages in the desktop app

The desktop app also freezes `godseye-uav`'s runtime dependencies (among them fastapi, starlette,
uvicorn, pydantic, mcp, anyio, msgpack-rpc-python, msgpack, tornado, pyproj with its PROJ data, and
egm96) and their own dependencies, each under its own licence. Their licence files are in the
installed `*.dist-info` directories; the build does not yet copy them, or this file, into the app.
Collect them before giving a build to anyone.

---

## ISR-only

This project is **ISR-only**: it observes, classifies and reports. It contains no kinetic capability
and none may be added. Threat assessment produces sensor-posture advice only; command authority
stays with the operator.
