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

## ISR-only

This project is **ISR-only**: it observes, classifies and reports. It contains no kinetic capability
and none may be added. Threat assessment produces sensor-posture advice only; command authority
stays with the operator.
