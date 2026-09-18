# Reporting templates — SALUTE and INTREP

Reports are **structured artifacts you fill field by field**, never free-form prose. The server
produces the structured objects; your job is to fill the fields honestly and narrate them.

## SALUTE — one per contact

| Field | Meaning | Rules |
|---|---|---|
| **S**ize | How many, and what echelon | Count what you actually observed. "3 vehicles", not "a convoy" unless you counted |
| **A**ctivity | What it is doing | Observable behaviour only: "moving west at 8 m/s", "static, engines warm". Not intent |
| **L**ocation | Where the **contact** is | The contact's own position — never the observer's. Include the datum |
| **U**nit | Who it is, if determinable | Only from observable markings/configuration. `unknown` is a valid and common answer |
| **T**ime | When observed | Time of observation, not time of reporting |
| **E**quipment | What it is made of | The OB classification plus the cues that justify it |

### Worked example

```
S: 3 × tracked vehicles, 1 × wheeled support
A: Halted in dispersed laager, engines warm on IR, no movement in 12 min of observation
L: 33.7241N 051.7238E ±40 m (GPS nominal), terrain 1548 m MSL
U: Unknown — no visible markings at 25 px on target
T: 2026-09-17T04:12:33Z
E: Probable MBT (T-72 family) — hull/turret ratio and long gun tube visible at narrow FOV;
   support vehicle unclassified
Confidence: probable — 4 independent sightings over 12 min, IR + EO, slant range 1.8 km,
   25 px on target. Not confirmed: no markings resolved, single aspect angle only.
```

Note what the example does: it states what was seen, what was *not* resolved, and why the confidence
is what it is. `Unknown` appears twice and that is correct.

## Confidence

| Level | Justification required |
|---|---|
| `confirmed` | Multiple independent sightings, sufficient pixels on target for identification, corroborating cues (markings, configuration, behaviour). Never from one distant frame |
| `probable` | Consistent evidence supporting one classification, but a plausible alternative remains open |
| `possible` | Detected and roughly classified; too few pixels, too short a look, or degraded conditions |

Always cite the evidence: sightings count, sensor, slant range, pixels on target, time since last fix,
light and weather. A confidence level without cited evidence is an unsupported assertion.

## INTREP — one per mission

```
MISSION SUMMARY   what was tasked, what was flown, outcome in one paragraph
COVERAGE          % of the tasked area actually imaged — the figure flown, not the figure planned
TRACKS            every track id with classification, confidence, last known location and time
SENSOR CONDITIONS light, weather, sensor(s) used, anything that degraded collection
LOAL EVENTS       every loss-of-link: when, how long, which lost-link plan ran
GAPS              what you could NOT see, and why
```

### On COVERAGE

Report what was **flown**, not what was planned. If the plan was truncated — fuel, geofence, weather,
an abort — the coverage figure must reflect that and the summary must say why. Reporting planned
coverage as achieved coverage is the single most damaging error in an ISR report, because the consumer
believes ground was cleared when it was not.

### On GAPS

The most valuable section. A report that lists only what was found implies everything else was checked
and clear. Record:

- Area inside the tasked polygon that was not imaged, and why
- Contacts detected but not identified, and what was missing (pixels, angle, light)
- Time windows with no collection (lost link, RTB leg, sensor unavailable)
- Terrain-masked areas that line of sight never reached
- Anything observed under degraded GPS, with the confidence caveat

### Worked fragment

```
COVERAGE: 61% of tasked polygon (planned 100%). Grid search terminated at lane 9 of 14 on BINGO
          fuel; the northern third was not imaged.
GAPS:     Northern third of the AO (approx 33.728-33.735N) — not overwhelmed, simply not reached.
          TRK-004 detected at 3.1 km, never identified: 9 px on target at max narrow FOV, and
          standoff could not be reduced without entering the SA-6 envelope.
          No collection 04:31-04:36Z — link lost, hold-orbit lost-link plan executed, LOAL logged.
```

## Threat assessment

The score is **computed**, not narrated into existence: capability comes from the order-of-battle
library (engagement envelope, acquisition range, mobility) and intent from the indicators (posture,
movement toward an asset, pattern-of-life deviation, emissions). Each component is traceable.

Your job is to **explain what the model produced and cite its evidence**. Do not invent or adjust
scores, and do not add an engagement recommendation — **ISR only (M14)**: the system reports,
the operator decides. Any advice you give is about *sensor posture and self-protection*
(stand off, climb, change aspect, break contact), never about engaging.
