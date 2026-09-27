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
