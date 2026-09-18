# ISR doctrine reference

Detail behind the summary in SKILL.md §4. Read when planning a mission where the altitude, sensor or
geometry choice actually matters.

## Sensor footprint and resolution

For a camera of horizontal field of view `HFOV` at height `alt_agl` above the ground:

```
swath      = 2 · alt_agl · tan(HFOV / 2)          # ground width covered by one pass
GSD        ≈ swath / image_width_px               # ground sample distance: metres per pixel
lane_spacing = swath · (1 − overlap_pct)          # what the server derives for a grid search
```

You give the server `overlap_pct`; it derives the spacing. Never pass a lane spacing yourself — that is
how coverage gaps get baked into a plan that looks complete.

**Why overlap exists.** Zero overlap assumes perfect navigation, no roll, no wind drift and a flat
earth. Real passes wander. Overlap is the margin that stops a strip of ground from being silently
missed between two lanes.

| Overlap | Use |
|---|---|
| 20–30% | Open flat terrain, calm air, area search where speed matters |
| 30–50% | Default. Mixed terrain, some wind |
| 50–70% | Rough terrain, strong wind, or when a miss is expensive (SAR) |

**Pixels on target.** A rough working rule for what you can do with a contact, by pixels across its
long dimension:

| Pixels | What it supports |
|---|---|
| ~4 | Detection — "something is there" |
| ~12 | Classification — "a wheeled vehicle" |
| ~25 | Recognition — "a truck, not a tank" |
| ~50+ | Identification — a specific type |

If you need identification and the geometry gives you 12 pixels, do not fly closer into a threat ring —
narrow the FOV (§ cross-cue) or accept and report a lower confidence.

## Sensor selection

| Sensor | Strong at | Blind to |
|---|---|---|
| `scene` (EO) | Daylight detail, colour, markings, reading a scene | Darkness, smoke, haze, camouflage that matches the background |
| `infrared` (IR) | Night, warm objects, running engines, recently occupied structures, seeing through haze/smoke | Fine detail and markings; thermal crossover at dawn/dusk when everything equalises |
| `depth` | Verifying range and structure geometry | Not an intel product on its own |
| `segmentation` | Ground truth for verification and test | Not an intel product — it is the sim telling you the answer |

**Thermal crossover** happens around dawn and dusk when surfaces and their surroundings reach the same
temperature and IR contrast collapses. If a night mission runs into dawn, expect IR to degrade and plan
the EO transition.

## Sun geometry (M6)

The sun washes out any image shot toward it and throws long shadows that hide contacts.

- Keep the sun **behind the sensor**: orbit the arc that puts the drone between the sun and the target.
- Low sun (early/late) gives long shadows — excellent for *detecting* vertical objects and revealing
  vehicles under partial cover, poor for *identifying* surface detail.
- High sun (midday) gives flat, even illumination — good for identification, weak shadow cues.
- `uav_orbit_poi` chooses the sun-side arc and reports its reasoning. `sim_set_time` sets the scenario
  clock when you need a specific sun angle.

## Standoff and threat geometry (M5)

Every order-of-battle class carries an engagement envelope and an acquisition range. Standoff is
derived from the contact's envelope plus the pixel density you need — it is **server-derived** and
verified against line of sight. You do not choose it.

Two distinct radii matter:

- **Acquisition range** — where the system can *see* you. Entering it means you are observed.
- **Engagement envelope** — where it can *reach* you.

Being observed is not the same as being at risk, but for an ISR mission both matter: a contact that
knows it is being watched changes its behaviour, which corrupts the pattern-of-life picture.

**Slant range** is what matters, not map distance:

```
slant_range = sqrt(ground_distance² + (alt_agl − target_alt)²)
```

Climbing increases your standoff without moving the ground track — often the cleanest way to stay
outside an envelope while keeping eyes on.

**Terrain masking.** A ridgeline between you and a contact breaks both observation and threat. Use
`uav_los_check` and read the `model` field so you know what was actually accounted for.

## Wind and fuel (M15)

Burn rate depends on flight phase and on the **headwind component** along the ground track, not on
wind speed alone:

```
headwind = wind_speed · cos(wind_direction − ground_track)
```

A route flown out into a headwind and back with a tailwind does **not** cost the same as the still-air
estimate — the slow upwind leg spends more time burning than the fast downwind leg saves. A box or
grid pattern pays the headwind penalty on every upwind lane.

The dry-run uses the same integrator as the in-flight model, so trust its estimate — but re-check your
margin if the wind changes mid-mission.

## GPS degradation (M16)

Under degradation, reported position drifts from truth; under denial, it stops updating.

- Widen track-association tolerance — the same contact may appear displaced between frames.
- Lower the confidence of every fix taken in the window, and say so in the INTREP.
- Prefer relative geometry (bearing and slant range from a known point) over absolute lat/lon.
- A contact's *identity* stays valid; its *location* is what degrades.

## Pattern of life (M12)

Repeated observation of the same place builds a baseline — when vehicles arrive and leave, how many
are normally present, what routes are used. Intent assessment leans on **deviation** from that
baseline: a convoy that departs at an unusual hour, or presence where there is normally none, is more
informative than the same scene observed once.

This is why the track store and pattern-of-life database deliberately survive a `sim_reset`: the
baseline is the accumulated value, and wiping it throws away the thing that makes intent assessable.
