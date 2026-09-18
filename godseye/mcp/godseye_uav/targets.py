"""Target identification, persistent tracks, pattern-of-life, reporting.

Covers PLAN §4.6(a) order-of-battle library (M13a), §4.6(c) confidence with
cited evidence (M13c), §4.7 SALUTE/INTREP artifacts (M8), M11 persistent
tracks and M12 pattern-of-life.

Everything here is deterministic and evidence-driven: no value is asserted
without a citation saying which observation produced it. The harness narrates;
the numbers come from this module.

ISR-only (M14): this module identifies, correlates and reports. It contains no
engagement, targeting-for-strike or weaponeering logic of any kind. The weapon
ranges in the order-of-battle library describe what a contact can do *to the
observing UAV* — they exist for standoff and self-protection, nothing else.

Key invariant (live-probe regression): a contact's location is ALWAYS the
contact's own `geo_point` from the detection. The observer's position is used
only as evidence context (slant range, sensor geometry) and is NEVER
substituted for a contact's position. A detection with no geo_point produces
no track.

SALUTE: Size, Activity, Location, Unit, Time, Equipment.
"""
from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# §4.6(a) — order-of-battle library (M13a)
# --------------------------------------------------------------------------
# type -> capabilities, weapon ranges, mobility. Data-driven and extensible:
# append a row to OB_LIBRARY and it is immediately available to classification,
# standoff derivation (M5) and threat scoring (M13). Ranges are nominal
# open-source figures, rounded; they exist to drive UAV standoff and
# self-protection doctrine, not fire control.


@dataclass(frozen=True)
class ObClass:
    """One order-of-battle class (PLAN §4.6(a), M13a).

    weapon_range_m / weapon_ceiling_m describe the engagement envelope against
    an airborne ISR observer. acquisition_range_m is the sensor/acquisition
    range, which is deliberately distinct: a radar can *see* a UAV far beyond
    anything it can reach.
    """

    key: str
    name: str
    category: str
    role: str
    capabilities: tuple[str, ...]
    weapon_range_m: float
    weapon_min_range_m: float
    weapon_ceiling_m: float
    acquisition_range_m: float
    mobility: str                 # fixed|towed|wheeled|tracked|dismounted|naval|air
    mobility_speed_mps: float
    typical_unit_size: str
    typical_unit_count: int
    signature_cues: tuple[str, ...]
    emitter: bool
    threat_weight: float          # capability weight vs a UAV observer, 0..1
    size_m: float                 # typical longest dimension (drives pixel density)
    keywords: tuple[str, ...]

    @property
    def engages_air(self) -> bool:
        return self.weapon_range_m > 0.0 and self.weapon_ceiling_m > 0.0

    def to_dict(self) -> dict:
        return {
            "ob_class": self.key, "name": self.name, "category": self.category,
            "role": self.role, "capabilities": list(self.capabilities),
            "weapon_range_m": self.weapon_range_m,
            "weapon_min_range_m": self.weapon_min_range_m,
            "weapon_ceiling_m": self.weapon_ceiling_m,
            "acquisition_range_m": self.acquisition_range_m,
            "mobility": self.mobility,
            "mobility_speed_mps": self.mobility_speed_mps,
            "typical_unit_size": self.typical_unit_size,
            "typical_unit_count": self.typical_unit_count,
            "signature_cues": list(self.signature_cues),
            "emitter": self.emitter,
            "threat_weight": self.threat_weight,
            "size_m": self.size_m,
        }


def _ob(key, name, category, role, capabilities, weapon_range_m,
        weapon_ceiling_m, acquisition_range_m, mobility, mobility_speed_mps,
        typical_unit_size, typical_unit_count, signature_cues, emitter,
        threat_weight, size_m, keywords, weapon_min_range_m=0.0) -> ObClass:
    return ObClass(
        key=key, name=name, category=category, role=role,
        capabilities=tuple(capabilities), weapon_range_m=float(weapon_range_m),
        weapon_min_range_m=float(weapon_min_range_m),
        weapon_ceiling_m=float(weapon_ceiling_m),
        acquisition_range_m=float(acquisition_range_m), mobility=mobility,
        mobility_speed_mps=float(mobility_speed_mps),
        typical_unit_size=typical_unit_size,
        typical_unit_count=int(typical_unit_count),
        signature_cues=tuple(signature_cues), emitter=bool(emitter),
        threat_weight=float(threat_weight), size_m=float(size_m),
        keywords=tuple(keywords),
    )


#: The library. Ordered most-specific/most-dangerous first; ties in keyword
#: match length break toward the earlier row.
OB_LIBRARY: dict[str, ObClass] = {c.key: c for c in (
    # ---- surface-to-air missile systems ----
    _ob("sam_long_range", "long-range SAM system (S-300/SA-10 class)", "sam",
        "area air defence",
        ("engages aircraft and UAVs at long range", "cues and hands off to shorter-range SAMs",
         "search + engagement radar organic to the battalion"),
        75000, 27000, 150000, "wheeled", 15.0,
        "battalion: 1 engagement radar, 1 search radar, 8-12 TELs", 12,
        ("four large canister tubes erected vertically on a heavy 8x8 chassis",
         "separate rotating engagement radar within ~1 km of the TELs",
         "generator trailers and cable runs between vehicles in IR"),
        True, 1.00, 13.0,
        ("s300", "s-300", "sa-10", "grumble", "sa-20", "s400", "s-400", "long range sam")),
    _ob("sam_medium_range", "medium-range SAM battery (SA-6/2K12 class)", "sam",
        "point and area air defence",
        ("engages UAVs and aircraft in the medium band", "semi-active radar homing",
         "battery displaces after engagement"),
        24000, 12000, 75000, "tracked", 12.0,
        "battery: 4 TELs + 1 straight-flush target-acquisition radar", 5,
        ("tracked TEL carrying three canted rails",
         "dish-type illumination radar co-located within the battery",
         "tracked-chassis exhaust plumes hot in IR when generators run"),
        True, 0.95, 7.4,
        ("sa-6", "sa6", "gainful", "2k12", "kub", "sa-11", "buk", "sam", "tel",
         "surface to air", "sa-3", "goa")),
    _ob("sam_short_range", "short-range SAM / SHORAD (SA-15 Tor class)", "sam",
        "manoeuvre-force air defence",
        ("engages UAVs and munitions at short range", "shoot-on-the-move capable",
         "organic search and tracking radar on the same vehicle"),
        12000, 6000, 25000, "tracked", 17.0,
        "battery of 4 combat vehicles", 4,
        ("single tracked hull with a rotating radar drum above the turret",
         "vertical launch cells in the hull roof",
         "no separate radar vehicle — everything on one chassis"),
        True, 0.90, 7.5,
        ("sa-15", "tor", "gauntlet", "sa-8", "osa", "gecko", "shorad")),
    _ob("spaag_missile", "gun/missile SHORAD (Pantsir/SA-22 class)", "aaa",
        "close-in air defence",
        ("twin 30 mm guns plus short-range missiles", "engages small UAVs",
         "phased-array tracking radar on the turret"),
        20000, 15000, 36000, "wheeled", 20.0,
        "battery of 6 combat vehicles", 6,
        ("8x8 truck chassis with twin gun barrels flanking missile canisters",
         "flat phased-array panel on the rear of the turret",
         "gun barrels and engine deck bloom in IR after firing"),
        True, 0.90, 8.0,
        ("pantsir", "sa-22", "greyhound", "tunguska", "sa-19")),
    _ob("manpads", "MANPADS team", "sam",
        "low-altitude point defence",
        ("shoulder-launched IR-homing missile", "engages low, slow UAVs",
         "passive — no radar warning before launch"),
        5000, 3500, 8000, "dismounted", 1.5,
        "2-man team, several per manoeuvre company", 2,
        ("two-man dismounted team, one carrying a long tube on the shoulder",
         "team moves to and holds elevated or open ground",
         "tube and gripstock visible as a hot line in IR after launch"),
        False, 0.70, 1.5,
        ("manpads", "igla", "stinger", "sa-7", "strela", "sa-18", "mistral")),
    # ---- anti-aircraft artillery ----
    _ob("aaa_self_propelled", "self-propelled AAA (ZSU-23-4 class)", "aaa",
        "close-in air defence",
        ("quad 23 mm cannon", "organic gun-laying radar", "engages low-flying UAVs"),
        2500, 1500, 20000, "tracked", 13.0,
        "battery of 4 guns", 4,
        ("four thin barrels in a squat box turret on a tracked hull",
         "small dish radar folded on the turret rear",
         "barrel cluster glows in IR immediately after a burst"),
        True, 0.75, 6.5,
        ("zsu", "shilka", "zsu-23-4", "spaag")),
    _ob("aaa_towed", "towed AAA (ZU-23-2 class)", "aaa",
        "close-in air defence",
        ("twin 23 mm cannon", "optical sights only", "often truck-mounted"),
        2000, 1500, 3000, "towed", 0.0,
        "section of 2-6 guns around a defended point", 4,
        ("two barrels on a low two-wheeled carriage, often sandbagged",
         "no radar dish anywhere near the position",
         "crew and ready ammunition stacked beside the mount"),
        False, 0.60, 4.6,
        ("zu-23", "aaa", "flak", "towed aa", "anti aircraft gun")),
    # ---- radars / EW ----
    _ob("radar_acquisition", "air-surveillance / acquisition radar", "radar",
        "air picture and SAM cueing",
        ("detects and tracks aircraft and UAVs at long range",
         "hands off tracks to SAM engagement radars", "no organic weapon"),
        0, 0, 200000, "towed", 0.0,
        "radar company: 1-2 sets plus command shelter", 2,
        ("large rotating antenna array on a mast or trailer",
         "power generators and cable runs to a nearby shelter",
         "cleared, level revetted site with vehicle tracks radiating out"),
        True, 0.55, 12.0,
        ("radar", "ew radar", "acquisition radar", "p-18", "spoon rest",
         "surveillance radar", "search radar")),
    _ob("radar_counterbattery", "counter-battery radar", "radar",
        "artillery location",
        ("locates firing artillery from projectile tracks",
         "cues counter-fire", "no organic weapon"),
        0, 0, 40000, "wheeled", 15.0,
        "1-2 sets per artillery brigade", 2,
        ("flat rectangular phased-array panel tilted skyward on a truck bed",
         "sited behind the forward line of troops facing one sector",
         "panel face and generator hot in IR"),
        True, 0.45, 9.0,
        ("counterbattery", "counter battery", "arthur", "zoopark", "firefinder")),
    _ob("ew_jammer", "electronic-warfare / jamming system", "radar",
        # M14: role text is echoed verbatim into SALUTE/INTREP/THREATREP, so it
        # must describe the contact without borrowing kinetic vocabulary.
        "electronic warfare",
        ("jams GNSS and command links", "degrades UAV navigation and control",
         "high-power emitter"),
        0, 0, 50000, "wheeled", 15.0,
        "EW company: 2-4 systems", 3,
        ("multiple dish or log-periodic antennas on a telescoping mast",
         "large generator trailer beside the vehicle",
         "mast erected only while operating"),
        True, 0.50, 10.0,
        ("jammer", "jamming", "electronic warfare", "krasukha", "leer", "esm")),
    # ---- command and control ----
    _ob("c2_node", "command post / C2 node", "c2",
        "command and control",
        ("directs subordinate units", "collates the air and ground picture",
         "high-value intelligence node"),
        500, 300, 2000, "wheeled", 18.0,
        "command element: 2-6 vehicles plus security", 4,
        ("cluster of box-body vehicles under camouflage netting",
         "whip and mast antennas out of proportion to the vehicle count",
         "trodden paths and a vehicle laager pattern around the cluster"),
        True, 0.30, 7.0,
        ("command post", "command", "headquarters", "hq", "c2", "cp vehicle",
         "staff vehicle")),
    _ob("comms_relay", "communications relay / satcom terminal", "c2",
        "communications",
        ("retransmits unit nets", "satellite reachback", "persistent emitter"),
        0, 0, 1000, "towed", 0.0,
        "1-2 terminals per command node", 2,
        ("single large dish or mast on otherwise empty ground",
         "small generator and one guard vehicle",
         "sited on high ground for line of sight"),
        True, 0.25, 5.0,
        ("relay", "satcom", "comms", "antenna mast", "retrans")),
    # ---- armour ----
    _ob("mbt", "main battle tank", "armor",
        "armoured manoeuvre",
        ("120/125 mm main gun against surface targets",
         "roof machine gun can reach very low, slow aircraft",
         "thermal sights out to several km"),
        1500, 800, 5000, "tracked", 10.0,
        "platoon of 3, company of 10", 3,
        ("long gun barrel overhanging the hull front",
         "wide tracks and a low rounded or wedge turret",
         "engine deck is the hottest object in the IR scene at halt"),
        False, 0.50, 9.5,
        ("t-72", "t72", "t-90", "t90", "t-80", "t80", "mbt", "tank", "abrams",
         "leopard", "challenger", "m1a2")),
    _ob("ifv", "infantry fighting vehicle", "armor",
        "mechanised infantry",
        ("autocannon usable against low-flying UAVs", "carries a dismount squad",
         "ATGM rail on some variants"),
        2500, 500, 4000, "tracked", 12.0,
        "platoon of 3 carrying an infantry platoon", 3,
        ("small turret with a thin autocannon barrel, no large gun tube",
         "rear troop doors and firing ports along the hull sides",
         "dismounts seen debussing beside the vehicle"),
        False, 0.45, 6.7,
        ("bmp", "ifv", "bradley", "fighting vehicle", "bmd", "marder", "cv90")),
    _ob("apc", "armoured personnel carrier", "armor",
        "protected mobility",
        ("heavy machine gun", "carries a dismount squad", "wheeled road mobility"),
        1500, 200, 3000, "wheeled", 22.0,
        "platoon of 3-4", 4,
        ("boat-shaped 8x8 hull with a small machine-gun cupola",
         "side hatches rather than a rear ramp on some variants",
         "road column spacing of 50-100 m between vehicles"),
        False, 0.35, 7.6,
        ("btr", "apc", "m113", "stryker", "personnel carrier", "mrap")),
    # ---- artillery ----
    _ob("spg", "self-propelled howitzer", "artillery",
        "indirect fire",
        ("long-range indirect fire against ground targets",
         "no capability against aircraft", "shoot-and-scoot"),
        0, 0, 2000, "tracked", 10.0,
        "battery of 6 guns plus ammunition carriers", 6,
        ("large box turret with a long barrel and muzzle brake",
         "guns dispersed 100-200 m apart on a gun line facing one bearing",
         "scorched ground and blast marks forward of each piece"),
        False, 0.30, 11.0,
        ("2s19", "2s1", "2s3", "msta", "gvozdika", "akatsiya", "paladin",
         "self propelled howitzer", "sph", "spg")),
    _ob("towed_howitzer", "towed howitzer", "artillery",
        "indirect fire",
        ("indirect fire against ground targets", "no capability against aircraft",
         "requires a prime mover to displace"),
        0, 0, 1500, "towed", 0.0,
        "battery of 6 guns plus prime movers", 6,
        ("split trails spread into a V behind the piece",
         "prime-mover trucks parked a short distance behind the gun line",
         "ammunition pits dug beside each gun"),
        False, 0.25, 7.0,
        ("d-30", "d30", "howitzer", "m777", "towed gun", "m119")),
    _ob("mlrs", "multiple rocket launcher", "artillery",
        "massed indirect fire",
        ("salvo rocket fire against area targets", "no capability against aircraft",
         "displaces immediately after firing"),
        0, 0, 1500, "wheeled", 18.0,
        "battery of 6 launchers plus reload vehicles", 6,
        ("bundle of parallel rocket tubes on a truck bed",
         "reload trucks stationed one bound behind the firing point",
         "very large IR bloom and smoke pall for tens of seconds after a salvo"),
        False, 0.35, 7.4,
        ("bm-21", "bm21", "grad", "mlrs", "himars", "smerch", "uragan",
         "rocket launcher", "tos")),
    _ob("mortar", "mortar section", "artillery",
        "close indirect fire",
        ("short-range indirect fire", "no capability against aircraft",
         "often vehicle-portable"),
        0, 0, 800, "dismounted", 1.5,
        "section of 2-4 tubes", 3,
        ("short tube at high elevation on a baseplate",
         "ammunition boxes laid out in an arc behind the tube",
         "crew of 3-4 dismounts around each tube"),
        False, 0.20, 2.0,
        ("mortar", "2b11", "2b14")),
    # ---- logistics ----
    _ob("supply_truck", "logistics / cargo truck", "logistics",
        "sustainment",
        ("moves ammunition, stores and troops", "no organic weapon",
         "travels in road columns"),
        0, 0, 500, "wheeled", 20.0,
        "transport platoon of 4-8 in column", 6,
        ("flat cargo bed under a canvas tilt or open with crates",
         "regular column spacing on a route, not tactical dispersion",
         "cab and engine warm, cargo bed cold in IR"),
        False, 0.10, 7.5,
        ("ural", "kamaz", "truck", "lorry", "supply", "logistics", "cargo",
         "flatbed", "gaz")),
    _ob("fuel_tanker", "fuel / POL tanker", "logistics",
        "sustainment",
        ("moves bulk fuel", "high-value sustainment node", "no organic weapon"),
        0, 0, 500, "wheeled", 18.0,
        "POL section of 2-4 tankers", 3,
        ("cylindrical tank body with a rear hose reel and pump housing",
         "parked apart from other vehicles with a safety distance",
         "tank body thermally uniform and cool relative to the cab"),
        False, 0.12, 9.0,
        ("tanker", "fuel truck", "bowser", "pol vehicle", "refueller")),
    _ob("utility_vehicle", "light utility vehicle", "vehicle",
        "liaison and reconnaissance",
        ("moves small teams quickly", "may carry a pintle-mounted weapon",
         "high road speed"),
        800, 0, 800, "wheeled", 25.0,
        "pairs, rarely alone", 2,
        ("small open or soft-top 4x4 with no armour profile",
         "moves faster and more erratically than the column it accompanies",
         "single warm engine block, small IR footprint"),
        False, 0.12, 4.8,
        ("uaz", "humvee", "hmmwv", "jeep", "technical", "land rover",
         "utility vehicle", "vehicle")),
    # ---- personnel ----
    _ob("infantry_squad", "dismounted infantry", "personnel",
        "close combat",
        ("small arms effective against very low UAVs", "occupies and holds ground",
         "may carry MANPADS or ATGM"),
        600, 0, 1000, "dismounted", 1.5,
        "squad of 8-10; platoon of ~30", 9,
        ("individual warm point sources moving in loose file or line",
         "spacing of 5-15 m maintained between figures",
         "figures persist in IR long after vehicles have cooled"),
        False, 0.15, 1.8,
        ("infantry", "soldier", "squad", "troops", "dismount", "person",
         "personnel", "rifleman", "platoon", "man")),
    _ob("observation_post", "observation post / dismounted observer", "personnel",
        "reconnaissance",
        ("observes and reports", "cues indirect fire", "passive, hard to detect"),
        600, 0, 10000, "dismounted", 1.0,
        "2-4 man team", 3,
        ("static figures on elevated or commanding ground",
         "optics or a tripod-mounted device beside the position",
         "no vehicle within several hundred metres of the position"),
        False, 0.20, 1.8,
        ("observation post", "observer", "spotter", "sentry", "lookout",
         "recce team")),
    # ---- structures ----
    _ob("bunker", "bunker / hardened shelter", "structure",
        "protection",
        ("shelters personnel or equipment", "hardened against indirect fire",
         "fixed in place"),
        0, 0, 0, "fixed", 0.0,
        "one position within a defended locality", 1,
        ("earth-covered mound with a single dark entrance and blast wall",
         "spoil and revetment berms around the perimeter",
         "thermally lagging the surrounding ground by hours"),
        False, 0.20, 12.0,
        ("bunker", "shelter", "hardened", "revetment", "hangar", "casemate")),
    _ob("depot_ammo", "ammunition depot", "structure",
        "sustainment",
        ("stores ammunition", "high-value sustainment node", "fixed in place"),
        0, 0, 0, "fixed", 0.0,
        "depot site: multiple bunkers or stacks", 1,
        ("regular rows of identical bunkers or stacks with wide separation",
         "single controlled entrance and a perimeter fence",
         "few vehicles present except during resupply"),
        False, 0.15, 30.0,
        ("ammo", "ammunition", "depot", "magazine", "ammunition dump")),
    _ob("depot_fuel", "fuel depot / POL point", "structure",
        "sustainment",
        ("stores bulk fuel", "high-value sustainment node", "fixed in place"),
        0, 0, 0, "fixed", 0.0,
        "POL site: tanks plus pumping point", 1,
        ("circular storage tanks inside a bunded enclosure",
         "tanker vehicles queueing at a single pumping point",
         "tank contents thermally distinct from the empty freeboard in IR"),
        False, 0.15, 25.0,
        ("fuel depot", "pol point", "tank farm", "storage tank", "fuel dump")),
    _ob("bridge", "bridge / crossing point", "structure",
        "mobility",
        ("carries a route across an obstacle", "chokepoint for movement",
         "fixed in place"),
        0, 0, 0, "fixed", 0.0,
        "one crossing site", 1,
        ("linear span across water or a cut, carrying a road surface",
         "vehicle queueing and traffic control on both approaches",
         "deck retains heat differently from the surrounding ground"),
        False, 0.05, 80.0,
        ("bridge", "crossing", "pontoon", "ford", "causeway")),
    _ob("structure", "building / structure", "structure",
        "unspecified",
        ("shelters people or stores", "fixed in place",
         "significance depends on what occupies it"),
        0, 0, 0, "fixed", 0.0,
        "single structure", 1,
        ("regular rectilinear footprint with a roof line and shadow",
         "utility connections and an access track",
         "roof temperature tracks the diurnal cycle"),
        False, 0.05, 20.0,
        # The multi-token entries beat the generic single-token keywords they
        # contain ('tank' -> mbt, 'ship' -> patrol_boat), because match_ob ranks
        # a longer contiguous token run first. Without them a water tank was
        # classified as a main battle tank and inherited a 1500 m weapons
        # envelope — the same collision class the token rewrite was meant to
        # close, just one level up.
        ("building", "house", "structure", "warehouse", "hotel", "shed",
         "factory", "tower", "compound", "barracks",
         "water tank", "septic tank", "fish tank", "header tank",
         "shipping container", "ship container", "container")),
    # ---- naval / air ----
    _ob("patrol_boat", "fast patrol boat", "naval",
        "littoral patrol",
        ("gun armament usable against low aircraft", "high speed in the littoral",
         "surface search radar"),
        4000, 500, 15000, "naval", 15.0,
        "patrol group of 2-4 craft", 3,
        ("planing hull with a pronounced wake at speed",
         "gun mount forward of the bridge",
         "hot exhaust aft and a warm wake trailing the hull in IR"),
        True, 0.40, 30.0,
        ("patrol boat", "boat", "vessel", "ship", "corvette", "craft", "skiff")),
    _ob("rotary_wing", "military helicopter", "aircraft",
        "air manoeuvre",
        ("engages surface and slow air targets", "rapid repositioning",
         "can intercept a UAV at its operating altitude"),
        8000, 5000, 20000, "air", 80.0,
        "flight of 2-4 airframes", 2,
        ("rotor disc blur above a slim fuselage",
         "hovering or nap-of-the-earth flight profile",
         "engine exhaust is the dominant IR source in the scene"),
        True, 0.70, 18.0,
        ("helicopter", "heli", "mi-8", "mi-24", "hind", "rotary", "apache",
         "ka-52")),
    _ob("fixed_wing", "combat aircraft", "aircraft",
        # M14: see ew_jammer — role text reaches the reports verbatim.
        "air superiority and interception",
        ("engages air targets at long range", "radar and IR missiles",
         "can intercept a UAV anywhere in its envelope"),
        40000, 18000, 150000, "air", 250.0,
        "pair or four-ship", 2,
        ("swept planform with a persistent contrail at altitude",
         "sustained straight-line speed no surface contact can match",
         "very bright, small IR source from the engine nozzle"),
        True, 0.85, 17.0,
        ("fighter", "jet", "aircraft", "su-27", "su-35", "mig", "plane",
         "fixed wing", "f-16")),
    _ob("uav_contact", "unmanned aircraft (other)", "aircraft",
        "reconnaissance",
        ("observes and reports", "may be hostile, friendly or neutral",
         "deconfliction concern"),
        0, 0, 20000, "air", 30.0,
        "single airframe with a ground control station", 1,
        ("small planform holding a regular racetrack or lawnmower pattern",
         "constant altitude and speed over a long period",
         "very small IR signature, often below detection at range"),
        True, 0.30, 5.0,
        ("uav", "uas", "drone", "quadcopter", "orlan", "bayraktar")),
    # ---- explicitly civilian ----
    _ob("civilian_vehicle", "civilian vehicle", "civilian",
        "civil traffic",
        ("no military capability", "protected under the law of armed conflict",
         "records as a civilian pattern-of-life contact"),
        0, 0, 0, "wheeled", 25.0,
        "individual vehicles in civil traffic", 1,
        ("varied colours and body styles inconsistent with a military fleet",
         "follows the civil road network and obeys junction behaviour",
         "no antennas, camouflage, markings or tactical spacing"),
        False, 0.02, 4.5,
        ("sedan", "car", "bus", "civilian", "taxi", "tractor", "van",
         "motorcycle", "bicycle", "minibus", "pickup", "school bus")),
)}

#: Returned when nothing in the library matches. Asserts NO weapons envelope:
#: an unclassified contact never silently inherits someone else's range ring.
UNCLASSIFIED = _ob(
    "unclassified", "unclassified contact", "unknown", "unknown",
    ("insufficient evidence to assign an order-of-battle class",),
    0, 0, 0, "unknown", 0.0, "unknown", 1,
    ("no distinguishing cue resolved at the range and resolution achieved",),
    False, 0.10, 5.0, (),
)

#: Prudent standoff for a contact with no asserted envelope (M5). Explicitly a
#: default-for-safety, not a claim about the contact's capability.
UNCLASSIFIED_STANDOFF_M = 1500.0
MIN_STANDOFF_M = 300.0

#: Representative row for a coarse category, used when a Track carries only a
#: legacy category string and no ob_class.
CATEGORY_REPRESENTATIVE = {
    "sam": "sam_medium_range",
    "tel": "sam_short_range",       # legacy category: a TEL is a SAM launcher
    "aaa": "aaa_self_propelled",
    "radar": "radar_acquisition",
    "c2": "c2_node",
    "armor": "mbt",
    "artillery": "spg",
    "logistics": "supply_truck",
    "vehicle": "utility_vehicle",   # legacy category
    "personnel": "infantry_squad",
    "structure": "structure",
    "naval": "patrol_boat",
    "aircraft": "fixed_wing",
    "civilian": "civilian_vehicle",
    "unknown": "unclassified",
}

_TOKEN_RE = re.compile(r"[a-z]+|[0-9]+")


def _tokens(text: str) -> list[str]:
    """Lowercase word/designator tokens.

    Splits on every non-alphanumeric character AND on letter/digit boundaries,
    so 'SA-6', 'sa6' and 'SA_6' all tokenize to ['sa', '6']. Keywords go
    through the same tokenizer, so matching is whole-token: 'Motorcycle' can
    no longer match the SAM keyword 'tor', and 'Hotel' can no longer match
    'tel' (the substring bug this replaces).
    """
    return _TOKEN_RE.findall((text or "").lower())


def _contiguous(hay: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(hay):
        return False
    for i in range(len(hay) - len(needle) + 1):
        if hay[i:i + len(needle)] == needle:
            return True
    return False


def match_ob(name: str) -> tuple[ObClass, dict]:
    """Match an asset/mesh name to an order-of-battle class (M13a).

    Returns (entry, match_evidence). match_evidence cites the exact token
    sequence that justified the classification, so a SALUTE can say *why*.
    """
    raw = (name or "").strip()
    key = raw.lower().replace("-", "_").replace(" ", "_")
    if key in OB_LIBRARY:                      # explicit OB class key
        return OB_LIBRARY[key], {"matched": key, "rule": "explicit ob_class key",
                                 "specificity": 1.0}
    toks = _tokens(raw)
    best: ObClass | None = None
    best_rank: tuple[int, int] = (0, 0)
    best_kw = ""
    for entry in OB_LIBRARY.values():
        for kw in entry.keywords:
            kwt = _tokens(kw)
            if not _contiguous(toks, kwt):
                continue
            rank = (len(kwt), len(kw))
            if rank > best_rank:
                best, best_rank, best_kw = entry, rank, kw
    if best is None:
        return UNCLASSIFIED, {"matched": None, "rule": "no order-of-battle cue in name",
                              "specificity": 0.0, "tokens": toks}
    # multi-token designator matches (e.g. 'sa-6') are platform-level evidence;
    # single generic tokens (e.g. 'truck') are only class-level.
    specificity = 1.0 if best_rank[0] >= 2 else 0.6
    return best, {"matched": best_kw, "rule": "order-of-battle keyword match",
                  "specificity": specificity, "tokens": toks}


def ob_class(key: str) -> ObClass:
    """Look up one OB class by key (falls back to UNCLASSIFIED)."""
    return OB_LIBRARY.get(key, UNCLASSIFIED)


def ob_for_category(category: str) -> ObClass:
    """Representative OB row for a coarse category (legacy Track support)."""
    return OB_LIBRARY.get(CATEGORY_REPRESENTATIVE.get(category or "", ""), UNCLASSIFIED)


def classify(mesh_name: str) -> str:
    """Map a mesh/asset name to an order-of-battle category (M13a).

    Whole-token matching: unrecognised names return 'unknown' rather than
    inheriting a weapons envelope by substring accident.
    """
    return match_ob(mesh_name)[0].category


# --------------------------------------------------------------------------
# §4.6(c) — confidence with cited evidence per element (M13c)
# --------------------------------------------------------------------------
#: Ordered weakest -> strongest.
CONFIDENCE_LEVELS: tuple[str, ...] = ("possible", "probable", "confirmed")

#: Doctrinal caps: a tier can never exceed what the observation count supports.
_SIGHTING_CAP = ((3, "confirmed"), (2, "probable"), (0, "possible"))
_CONFIRMED_SCORE = 0.70
_PROBABLE_SCORE = 0.40

_SENSOR_QUALITY = {
    "segmentation": 1.00, "scene": 0.85, "eo": 0.85, "rgb": 0.85,
    "infrared": 0.80, "ir": 0.80, "thermal": 0.80, "depth": 0.50,
    "unknown": 0.40,
}
_LIGHT_FACTOR = {"day": 1.0, "dawn": 0.75, "dusk": 0.75, "night": 0.45}
_WEATHER_FACTOR = {"clear": 1.0, "haze": 0.85, "rain": 0.60, "snow": 0.55,
                   "dust": 0.40, "fog": 0.35}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _cite(element: str, value, source: str, *, weight: float | None = None,
          score: float | None = None, unit: str | None = None,
          inferred: bool = False) -> dict:
    """One citable evidence item: what was measured, and where it came from."""
    item: dict = {"element": element, "value": value, "source": source}
    if unit is not None:
        item["unit"] = unit
    if weight is not None:
        item["weight"] = round(weight, 3)
    if score is not None:
        item["score"] = round(float(score), 3)
    if weight is not None and score is not None:
        item["contribution"] = round(weight * float(score), 4)
    if inferred:
        item["inferred"] = True
    return item


def level_index(level: str) -> int:
    """Index of a confidence level (higher = stronger)."""
    try:
        return CONFIDENCE_LEVELS.index(level)
    except ValueError:
        return 0


def confidence_at_least(level: str, minimum: str) -> bool:
    """True if `level` is at least as strong as `minimum`."""
    return level_index(level) >= level_index(minimum)


@dataclass
class Observation:
    """One sensor look that produced a fix — the citable unit of evidence.

    `lat/lon/alt_m` are the CONTACT's own reported position. Observer geometry
    lives in slant_range_m / observer, never in the position fields (M13c).
    """

    ts: float
    lat: float
    lon: float
    alt_m: float = 0.0
    sensor: str = "scene"
    slant_range_m: float | None = None
    pixels_on_target: float | None = None
    fov_deg: float | None = None
    image_px: int = 640
    light: str = "day"
    weather: str = "clear"
    observer: str = "UAV"
    frame_id: str | None = None
    emitter_active: bool | None = None
    geo_source: str = "detection.geo_point"

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Observation":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def sensor_score(self) -> tuple[float, str]:
        """Sensor/condition quality of this look, with a human-readable basis."""
        base = _SENSOR_QUALITY.get((self.sensor or "unknown").lower(), 0.4)
        light = _LIGHT_FACTOR.get((self.light or "day").lower(), 0.6)
        if (self.sensor or "").lower() in ("infrared", "ir", "thermal"):
            light = max(light, 0.9)  # IR is not limited by visible light
        weather = _WEATHER_FACTOR.get((self.weather or "clear").lower(), 0.6)
        return (_clamp(base * light * weather),
                f"{self.sensor} sensor in {self.light}/{self.weather} conditions")

    def resolution_score(self, target_size_m: float) -> tuple[float, str, float | None]:
        """Pixels-on-target score. Uses measured pixels if the detector gave
        them, else derives them from slant range and FOV (M7 cross-cue math)."""
        px = self.pixels_on_target
        basis = "measured pixels on target from the detection box"
        if px is None and self.slant_range_m and self.fov_deg:
            gsd = 2.0 * self.slant_range_m * math.tan(math.radians(self.fov_deg) / 2.0) / max(1, self.image_px)
            px = target_size_m / gsd if gsd > 0 else None
            basis = (f"derived from slant {self.slant_range_m:.0f} m, FOV "
                     f"{self.fov_deg:.0f} deg, {self.image_px} px across")
        if px is None:
            if self.slant_range_m is not None:
                return (_clamp(1.0 - self.slant_range_m / 5000.0) * 0.8,
                        f"slant range {self.slant_range_m:.0f} m only (no FOV reported)",
                        None)
            return 0.35, "no sensor geometry reported with the detection", None
        return _clamp((px - 3.0) / 27.0), basis, px


# --------------------------------------------------------------------------
# M11 — persistent tracks with durable, non-reusable ids
# --------------------------------------------------------------------------
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford-ish: no I, L, O, U


def _b32(n: int, width: int = 1) -> str:
    n = max(0, int(n))
    out = ""
    while n:
        n, r = divmod(n, 32)
        out = _B32[r] + out
    return (out or "0").rjust(width, "0")


#: Entropy bits mixed in below the clock in a run prefix. The first cut of M11
#: used 10, leaving a 1-in-1024 chance that two runs started in the same second
#: minted the SAME prefix — and a collided prefix re-issues `TRK-<prefix>-0001`
#: for a different object, which is precisely the failure M11 exists to
#: prevent. 40 bits puts a same-second pair at ~1 in 1.1e12, and keeps the
#: birthday probability negligible even across thousands of restarts.
_ORIGIN_ENTROPY_BITS = 40
_ORIGIN_WIDTH = 12       # ceil((20 clock + 40 entropy) / 5) base-32 characters


def mint_origin(now: float | None = None) -> str:
    """Run-unique track-id prefix (M11).

    20 bits of clock + `_ORIGIN_ENTROPY_BITS` of entropy. Two server runs
    started in the same second differ only in the entropy field, so that field
    must be wide enough to make a collision negligible rather than merely
    unlikely: a track id from one run must never silently denote a different
    object in another. Restart continuity comes from reloading persisted state,
    which restores the original prefix.
    """
    secs = int(now if now is not None else time.time())
    mask = (1 << _ORIGIN_ENTROPY_BITS) - 1
    return _b32(((secs & 0xFFFFF) << _ORIGIN_ENTROPY_BITS)
                | (uuid.uuid4().int & mask), width=_ORIGIN_WIDTH)


ELEMENT_RADIUS_M = 500.0     # SALUTE 'Size': how far apart peers can be
_DWELL_RADIUS_M = 25.0       # movement below this counts as stationary


def _ground_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Ground distance in metres (local equirectangular; <1 m error in an AO)."""
    m_lon = 111320.0 * math.cos(math.radians(a_lat))
    return math.hypot((b_lat - a_lat) * 111320.0, (b_lon - a_lon) * m_lon)


@dataclass
class Track:
    """A persistent, numbered contact (M11).

    Position fields are always the contact's own reported geo_point. Nothing
    in this class ever writes an observer position into lat/lon/alt_m.
    """

    track_id: str
    name: str
    category: str
    lat: float
    lon: float
    alt_m: float
    first_seen: float
    last_seen: float
    sightings: int = 1
    heading_deg: float | None = None
    speed_mps: float | None = None
    # pattern-of-life: recent (t, lat, lon) breadcrumbs (M12)
    history: list = field(default_factory=list)
    # --- M11 durability / M13 evidence ---
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    origin_run: str = ""
    ob_class: str = "unclassified"
    match_evidence: dict = field(default_factory=dict)
    observations: list = field(default_factory=list)   # list[Observation]
    sim_epoch: int = 0
    max_observations: int = 50

    # ---- derived ----
    @property
    def ob(self) -> ObClass:
        """Order-of-battle row backing this track (M13a).

        Falls back to the category's representative row so tracks built before
        ob_class existed still resolve to a real library entry.
        """
        if self.ob_class in OB_LIBRARY:
            return OB_LIBRARY[self.ob_class]
        return ob_for_category(self.category)

    @property
    def track_age_s(self) -> float:
        return max(0.0, self.last_seen - self.first_seen)

    def staleness_s(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, now - self.last_seen)

    def dwell_s(self, now: float | None = None) -> float:
        """How long the contact has been within _DWELL_RADIUS_M of its fix."""
        if not self.history:
            return 0.0
        now = self.last_seen if now is None else now
        anchor_t = self.history[-1][0]
        for t, la, lo in reversed(self.history):
            if _ground_m(la, lo, self.lat, self.lon) > _DWELL_RADIUS_M:
                break
            anchor_t = t
        return max(0.0, now - anchor_t)

    # ---- ingest ----
    def update(self, lat: float, lon: float, alt_m: float, now: float,
               observation: Observation | None = None) -> None:
        """Fold one new fix in. lat/lon/alt_m are the CONTACT's own position."""
        if self.history:
            pt, plat, plon = self.history[-1]
            dt = max(1e-3, now - pt)
            m_lat = 111320.0
            m_lon = 111320.0 * math.cos(math.radians(plat))
            dn = (lat - plat) * m_lat
            de = (lon - plon) * m_lon
            dist = math.hypot(dn, de)
            if dist > 0.5:  # ignore jitter
                self.speed_mps = round(dist / dt, 2)
                self.heading_deg = round(math.degrees(math.atan2(de, dn)) % 360, 1)
        self.lat, self.lon, self.alt_m = lat, lon, alt_m
        self.last_seen = now
        self.sightings += 1
        self.history.append((now, lat, lon))
        if len(self.history) > 200:
            self.history.pop(0)
        if observation is not None:
            self.add_observation(observation)

    def add_observation(self, obs: Observation) -> None:
        self.observations.append(obs)
        if len(self.observations) > self.max_observations:
            self.observations.pop(0)

    def best_observation(self) -> Observation | None:
        """The look with the strongest sensor/resolution evidence (M13c)."""
        if not self.observations:
            return None
        size = self.ob.size_m
        return max(self.observations,
                   key=lambda o: (o.sensor_score()[0] + o.resolution_score(size)[0]))

    def emitter_observed(self) -> bool:
        return any(o.emitter_active for o in self.observations)

    # ---- (de)serialization for store.py persistence (M11/M12) ----
    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id, "uid": self.uid,
            "origin_run": self.origin_run, "name": self.name,
            "category": self.category, "ob_class": self.ob_class,
            "match_evidence": self.match_evidence,
            "lat": self.lat, "lon": self.lon, "alt_m": self.alt_m,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "sightings": self.sightings, "heading_deg": self.heading_deg,
            "speed_mps": self.speed_mps, "sim_epoch": self.sim_epoch,
            "history": [list(h) for h in self.history],
            "observations": [o.to_dict() for o in self.observations],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        t = cls(
            track_id=d["track_id"], name=d.get("name", "unknown"),
            category=d.get("category", "unknown"),
            lat=float(d["lat"]), lon=float(d["lon"]),
            alt_m=float(d.get("alt_m", 0.0)),
            first_seen=float(d.get("first_seen", 0.0)),
            last_seen=float(d.get("last_seen", 0.0)),
            sightings=int(d.get("sightings", 1)),
            heading_deg=d.get("heading_deg"), speed_mps=d.get("speed_mps"),
            uid=d.get("uid") or uuid.uuid4().hex,
            origin_run=d.get("origin_run", ""),
            ob_class=d.get("ob_class", "unclassified"),
            match_evidence=d.get("match_evidence") or {},
            sim_epoch=int(d.get("sim_epoch", 0)),
        )
        t.history = [tuple(h) for h in d.get("history", [])]
        t.observations = [Observation.from_dict(o) for o in d.get("observations", [])]
        return t


def assess_confidence(track: Track, now: float | None = None) -> dict:
    """Confidence in a track's identification, per PLAN §4.6(c) / M13(c).

    Derived from real evidence — independent sightings, observation span,
    sensor type and conditions, pixel density / slant range at detection,
    time since last fix and how specific the OB match was — never a constant.
    Every factor returns a citation saying which observation produced it.
    """
    now = time.time() if now is None else now
    ob = track.ob
    ev: list[dict] = []

    n = max(1, int(track.sightings))
    s_sight = _clamp((n - 1) / 5.0)
    ev.append(_cite(
        "independent_sightings", n,
        f"track {track.track_id}: {n} correlated fix(es) between "
        f"t={track.first_seen:.0f} and t={track.last_seen:.0f}",
        weight=0.24, score=s_sight, unit="fixes"))

    span = track.track_age_s
    s_span = _clamp(span / 120.0)
    ev.append(_cite(
        "observation_span_s", round(span, 1),
        f"track {track.track_id}: {span:.0f} s of continuous custody "
        "(multi-aspect looks reduce single-look error)",
        weight=0.12, score=s_span, unit="s"))

    best = track.best_observation()
    if best is not None:
        s_sensor, sensor_basis = best.sensor_score()
        frame = best.frame_id or f"t={best.ts:.0f}"
        ev.append(_cite("sensor_conditions", sensor_basis,
                        f"best look on track {track.track_id} (frame {frame})",
                        weight=0.16, score=s_sensor))
        s_res, res_basis, px = best.resolution_score(ob.size_m)
        ev.append(_cite("pixels_on_target", round(px, 1) if px else None,
                        f"{res_basis} (frame {frame}), target nominal "
                        f"{ob.size_m:.1f} m",
                        weight=0.20, score=s_res, unit="px"))
    else:
        s_sensor, s_res = 0.40, 0.35
        ev.append(_cite("sensor_conditions", "unreported",
                        "detection carried no sensor metadata", weight=0.16,
                        score=s_sensor, inferred=True))
        ev.append(_cite("pixels_on_target", None,
                        "detection carried no slant range or FOV", weight=0.20,
                        score=s_res, inferred=True))

    stale = track.staleness_s(now)
    s_recent = _clamp(1.0 - stale / 600.0)
    ev.append(_cite("time_since_last_fix_s", round(stale, 1),
                    f"last fix on track {track.track_id} at t={track.last_seen:.0f}",
                    weight=0.12, score=s_recent, unit="s"))

    spec = float(track.match_evidence.get("specificity", 0.0 if ob is UNCLASSIFIED else 0.6))
    matched = track.match_evidence.get("matched")
    ev.append(_cite("classification_specificity", ob.key,
                    (f"order-of-battle keyword '{matched}' matched in asset name "
                     f"'{track.name}'") if matched else
                    f"no order-of-battle cue in asset name '{track.name}'",
                    weight=0.16, score=spec))

    score = sum(i["weight"] * i["score"] for i in ev)
    tier = ("confirmed" if score >= _CONFIRMED_SCORE
            else "probable" if score >= _PROBABLE_SCORE else "possible")

    # Doctrinal caps: however good the imagery, a tier can never exceed what
    # the observation count and the classification itself support.
    sighting_cap = next(name for need, name in _SIGHTING_CAP if n >= need)
    class_cap = "probable" if spec <= 0.0 else "confirmed"
    cap = min((sighting_cap, class_cap), key=level_index)
    level = min((tier, cap), key=level_index)
    if level_index(cap) < level_index(tier):
        if level_index(sighting_cap) <= level_index(class_cap):
            ev.append(_cite("sighting_cap", sighting_cap,
                            f"doctrinal cap: {n} sighting(s) supports at most "
                            f"'{sighting_cap}' regardless of score "
                            f"('{tier}' withheld)", score=0.0))
        else:
            ev.append(_cite("classification_cap", class_cap,
                            "doctrinal cap: a contact with no order-of-battle "
                            f"match cannot be identified better than "
                            f"'{class_cap}' ('{tier}' withheld)", score=0.0))
    return {
        "level": level, "score": round(score, 3),
        "score_tier": tier, "sighting_cap": sighting_cap,
        "classification_cap": class_cap,
        "thresholds": {"confirmed": _CONFIRMED_SCORE, "probable": _PROBABLE_SCORE},
        "evidence": ev,
    }


class TrackManager:
    """Correlates detections into persistent tracks with durable ids (M11).

    Ids are `TRK-<run prefix>-<seq>`. The run prefix makes them non-reusable
    across restarts even with no persistence; `to_dict`/`from_dict` restore an
    earlier run's tracks and their original ids so a track id always denotes
    the same object. `mark_sim_reset` bumps the sim epoch WITHOUT dropping
    tracks (M12: the store survives sim_reset).
    """

    def __init__(self, associate_radius_m: float = 75.0,
                 origin: str | None = None, state: dict | None = None):
        self.associate_radius_m = associate_radius_m
        self._tracks: dict[str, Track] = {}
        self.origin = origin or mint_origin()
        self._seq = 0
        self.sim_epoch = 0
        self._minted: set[str] = set()
        #: detections dropped because they carried no contact position (never
        #: back-filled from the observer) — auditable.
        self.rejected: list[dict] = []
        if state:
            self.load_state(state)

    # ---- durable ids ----
    def new_track_id(self) -> str:
        while True:
            self._seq += 1
            tid = f"TRK-{self.origin}-{self._seq:04d}"
            if tid not in self._minted and tid not in self._tracks:
                self._minted.add(tid)
                return tid

    def _associate(self, lat: float, lon: float) -> Track | None:
        best, best_d = None, self.associate_radius_m
        for t in self._tracks.values():
            d = _ground_m(t.lat, t.lon, lat, lon)
            if d < best_d:
                best, best_d = t, d
        return best

    def ingest(self, detections: list[dict], now: float | None = None,
               sensor: dict | None = None, observer: dict | None = None,
               frame_id: str | None = None) -> list[Track]:
        """Fold one frame of DetectionInfo dicts into tracks; return updates.

        `observer` ({lat, lon, alt_m, vehicle}) is used ONLY to derive evidence
        geometry (slant range) for the confidence model. It is never written
        into a contact's position: a detection with no `geo_point` is rejected,
        not back-filled from the observer (live-probe regression, M13c).
        """
        now = now if now is not None else time.time()
        sensor = sensor or {}
        updated = []
        for det in detections:
            gp = det.get("geo_point") or {}
            lat, lon = gp.get("latitude"), gp.get("longitude")
            name = det.get("name", "unknown")
            if lat is None or lon is None:
                self.rejected.append({
                    "name": name, "ts": now,
                    "reason": "detection carried no geo_point; observer position "
                              "is never substituted for a contact position",
                })
                continue
            alt = gp.get("altitude", 0.0)
            obs = self._observation(det, lat, lon, alt, now, sensor, observer, frame_id)
            track = self._associate(lat, lon)
            if track is None:
                entry, match_ev = match_ob(name)
                track = Track(
                    track_id=self.new_track_id(), name=name,
                    category=entry.category, lat=lat, lon=lon, alt_m=alt,
                    first_seen=now, last_seen=now,
                    origin_run=self.origin, ob_class=entry.key,
                    match_evidence=match_ev, sim_epoch=self.sim_epoch,
                )
                track.history.append((now, lat, lon))  # seed first fix
                track.add_observation(obs)
                self._tracks[track.track_id] = track
            else:
                track.update(lat, lon, alt, now, observation=obs)
                track.sim_epoch = self.sim_epoch
                if track.ob_class in ("unclassified", ""):
                    entry, match_ev = match_ob(name)
                    if entry is not UNCLASSIFIED:
                        track.ob_class, track.category = entry.key, entry.category
                        track.match_evidence = match_ev
            updated.append(track)
        return updated

    def _observation(self, det: dict, lat: float, lon: float, alt: float,
                     now: float, sensor: dict, observer: dict | None,
                     frame_id: str | None) -> Observation:
        slant = det.get("slant_range_m")
        if slant is None and observer and observer.get("lat") is not None:
            ground = _ground_m(float(observer["lat"]), float(observer["lon"]), lat, lon)
            dz = float(observer.get("alt_m", 0.0)) - float(alt or 0.0)
            slant = math.hypot(ground, dz)
        return Observation(
            ts=now, lat=lat, lon=lon, alt_m=float(alt or 0.0),
            sensor=det.get("sensor") or sensor.get("sensor", "scene"),
            slant_range_m=slant,
            pixels_on_target=det.get("pixels_on_target"),
            fov_deg=det.get("fov_deg", sensor.get("fov_deg")),
            image_px=int(det.get("image_px", sensor.get("image_px", 640))),
            light=sensor.get("light", "day"), weather=sensor.get("weather", "clear"),
            observer=(observer or {}).get("vehicle", "UAV"),
            frame_id=frame_id, emitter_active=det.get("emitter_active"),
        )

    def tracks(self) -> list[Track]:
        return list(self._tracks.values())

    def get(self, track_id: str) -> Track | None:
        return self._tracks.get(track_id)

    def add(self, track: Track) -> Track:
        """Insert a pre-built track (restore path); never reassigns its id."""
        self._tracks[track.track_id] = track
        self._minted.add(track.track_id)
        return track

    def mark_sim_reset(self, now: float | None = None) -> dict:
        """sim_reset happened: bump the sim epoch, keep every track (M12).

        PLAN §4.4: sim_reset does NOT wipe the track store or pattern-of-life.
        """
        self.sim_epoch += 1
        return {"sim_epoch": self.sim_epoch, "tracks_retained": len(self._tracks),
                "at": now if now is not None else time.time()}

    # ---- (de)serialization; store.py owns the file (see handoff) ----
    def to_dict(self) -> dict:
        return {
            "version": 1, "origin": self.origin, "seq": self._seq,
            "sim_epoch": self.sim_epoch,
            "associate_radius_m": self.associate_radius_m,
            "tracks": [t.to_dict() for t in self._tracks.values()],
        }

    def load_state(self, state: dict) -> int:
        """Restore persisted tracks. Ids are preserved verbatim; the manager
        keeps its own (new) mint prefix so nothing is ever re-issued."""
        for row in state.get("tracks", []):
            try:
                self.add(Track.from_dict(row))
            except (KeyError, TypeError, ValueError):
                continue  # tolerate a corrupt row, same as the JSONL journals
        self.sim_epoch = max(self.sim_epoch, int(state.get("sim_epoch", 0)))
        if state.get("origin") == self.origin:
            self._seq = max(self._seq, int(state.get("seq", 0)))
        return len(self._tracks)

    @classmethod
    def from_dict(cls, state: dict, associate_radius_m: float | None = None) -> "TrackManager":
        tm = cls(associate_radius_m=associate_radius_m
                 if associate_radius_m is not None
                 else float(state.get("associate_radius_m", 75.0)))
        tm.load_state(state)
        return tm


# --------------------------------------------------------------------------
# M12 — pattern-of-life store + deviation metric
# --------------------------------------------------------------------------
@dataclass
class PoiBaseline:
    """Observed normal activity at one point of interest (M12).

    Activity is binned by UTC hour-of-day, by contact category, and by dwell
    duration. Deviation from this baseline is an intent indicator in §4.6(b).
    """

    poi: str
    lat: float
    lon: float
    radius_m: float = 250.0
    hourly: list = field(default_factory=lambda: [0] * 24)
    categories: dict = field(default_factory=dict)
    dwell_samples: list = field(default_factory=list)
    visits: int = 0
    total_obs: int = 0
    first_obs: float | None = None
    last_obs: float | None = None
    open_visits: dict = field(default_factory=dict)   # track_id -> arrival ts

    def contains(self, lat: float, lon: float) -> bool:
        return _ground_m(self.lat, self.lon, lat, lon) <= self.radius_m

    def mean_dwell_s(self) -> float | None:
        return (sum(self.dwell_samples) / len(self.dwell_samples)
                if self.dwell_samples else None)

    def to_dict(self) -> dict:
        return {
            "poi": self.poi, "lat": self.lat, "lon": self.lon,
            "radius_m": self.radius_m, "hourly": list(self.hourly),
            "categories": dict(self.categories),
            "dwell_samples": list(self.dwell_samples), "visits": self.visits,
            "total_obs": self.total_obs, "first_obs": self.first_obs,
            "last_obs": self.last_obs, "open_visits": dict(self.open_visits),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PoiBaseline":
        b = cls(poi=d["poi"], lat=float(d["lat"]), lon=float(d["lon"]),
                radius_m=float(d.get("radius_m", 250.0)))
        b.hourly = list(d.get("hourly") or [0] * 24)
        b.categories = dict(d.get("categories") or {})
        b.dwell_samples = list(d.get("dwell_samples") or [])
        b.visits = int(d.get("visits", 0))
        b.total_obs = int(d.get("total_obs", 0))
        b.first_obs, b.last_obs = d.get("first_obs"), d.get("last_obs")
        b.open_visits = dict(d.get("open_visits") or {})
        return b


class PatternOfLife:
    """Per-POI pattern-of-life store + deviation metric (M12, PLAN §4.8).

    Feeds the §4.6(b) 'pattern-of-life deviation' intent indicator and backs
    `uav://pattern-of-life/{poi}`. Deliberately independent of TrackManager so
    it can be persisted and reloaded on its own — and so `sim_reset` can leave
    it untouched (PLAN §4.4: sim_reset does NOT wipe the pattern-of-life DB).
    """

    def __init__(self, min_samples: int = 24):
        self.min_samples = max(1, int(min_samples))
        self._pois: dict[str, PoiBaseline] = {}
        self.sim_resets: list = []

    # ---- POIs ----
    def define_poi(self, poi: str, lat: float, lon: float,
                   radius_m: float = 250.0) -> PoiBaseline:
        b = self._pois.get(poi)
        if b is None:
            b = PoiBaseline(poi=poi, lat=lat, lon=lon, radius_m=radius_m)
            self._pois[poi] = b
        else:
            b.lat, b.lon, b.radius_m = lat, lon, radius_m
        return b

    def pois(self) -> list[str]:
        return list(self._pois)

    def get(self, poi: str) -> PoiBaseline | None:
        return self._pois.get(poi)

    def pois_containing(self, lat: float, lon: float) -> list[str]:
        return [name for name, b in self._pois.items() if b.contains(lat, lon)]

    # ---- accumulate ----
    def observe(self, track_id: str, category: str, lat: float, lon: float,
                ts: float | None = None) -> list[str]:
        """Record one fix against every POI that contains it. Returns POI names."""
        ts = time.time() if ts is None else ts
        hit = []
        for name, b in self._pois.items():
            if b.contains(lat, lon):
                hit.append(name)
                b.total_obs += 1
                b.hourly[time.gmtime(ts).tm_hour] += 1
                b.categories[category] = b.categories.get(category, 0) + 1
                b.first_obs = ts if b.first_obs is None else min(b.first_obs, ts)
                b.last_obs = ts if b.last_obs is None else max(b.last_obs, ts)
                if track_id not in b.open_visits:
                    b.open_visits[track_id] = ts
                    b.visits += 1
            elif track_id in b.open_visits:
                arrived = b.open_visits.pop(track_id)
                b.dwell_samples.append(max(0.0, ts - arrived))
                if len(b.dwell_samples) > 200:
                    b.dwell_samples.pop(0)
        return hit

    def observe_track(self, track: Track, ts: float | None = None) -> list[str]:
        return self.observe(track.track_id, track.category, track.lat, track.lon,
                            ts if ts is not None else track.last_seen)

    # ---- deviation metric ----
    def deviation(self, poi: str, category: str | None = None,
                  ts: float | None = None, dwell_s: float | None = None) -> dict:
        """How unusual is this activity at `poi`, 0 (normal) .. 1 (unprecedented).

        Components (each cited): hour-of-day activity share, category novelty,
        and dwell anomaly. The result is scaled by baseline maturity so a thin
        baseline cannot manufacture a deviation.
        """
        b = self._pois.get(poi)
        ts = time.time() if ts is None else ts
        if b is None:
            return {"poi": poi, "deviation": 0.0, "mature": False,
                    "baseline_samples": 0,
                    "evidence": [_cite("baseline", None, f"no baseline for POI {poi}",
                                       score=0.0)]}
        ev: list[dict] = []
        parts: list[tuple[float, float]] = []   # (weight, score)

        hour = time.gmtime(ts).tm_hour
        if b.total_obs:
            share = b.hourly[hour] / b.total_obs
            act = _clamp(1.0 - share / (1.0 / 24.0))
        else:
            share, act = 0.0, 0.0
        ev.append(_cite("hour_of_day_activity", round(share, 4),
                        f"POI {poi}: {b.hourly[hour]} of {b.total_obs} baseline "
                        f"observations fall in UTC hour {hour:02d}",
                        weight=0.45, score=act, unit="share"))
        parts.append((0.45, act))

        if category is not None and b.total_obs:
            seen = b.categories.get(category, 0)
            cat = _clamp(1.0 - seen / b.total_obs)
            ev.append(_cite("category_novelty", seen,
                            f"POI {poi}: category '{category}' seen {seen} of "
                            f"{b.total_obs} baseline observations",
                            weight=0.35, score=cat, unit="observations"))
            parts.append((0.35, cat))

        mean = b.mean_dwell_s()
        if dwell_s is not None and mean is not None and mean > 0:
            anomaly = _clamp(abs(dwell_s - mean) / max(mean, 60.0))
            ev.append(_cite("dwell_anomaly", round(dwell_s, 1),
                            f"POI {poi}: baseline mean dwell {mean:.0f} s over "
                            f"{len(b.dwell_samples)} completed visits",
                            weight=0.20, score=anomaly, unit="s"))
            parts.append((0.20, anomaly))

        total_w = sum(w for w, _ in parts) or 1.0
        raw = sum(w * s for w, s in parts) / total_w
        maturity = _clamp(b.total_obs / self.min_samples)
        ev.append(_cite("baseline_maturity", b.total_obs,
                        f"POI {poi}: {b.total_obs} of {self.min_samples} "
                        "observations needed for a mature baseline",
                        score=maturity, unit="observations"))
        return {
            "poi": poi, "deviation": round(raw * maturity, 3),
            "raw_deviation": round(raw, 3), "maturity": round(maturity, 3),
            "mature": b.total_obs >= self.min_samples,
            "baseline_samples": b.total_obs, "hour_utc": hour,
            "evidence": ev,
        }

    def deviation_for_track(self, track: Track, now: float | None = None) -> dict:
        """Worst POI deviation for where this track currently is (M12 -> §4.6b)."""
        now = track.last_seen if now is None else now
        names = self.pois_containing(track.lat, track.lon)
        if not names:
            return {"poi": None, "deviation": 0.0, "mature": False,
                    "baseline_samples": 0,
                    "evidence": [_cite("pattern_of_life", None,
                                       f"track {track.track_id} is not inside any "
                                       "pattern-of-life POI", score=0.0)]}
        results = [self.deviation(n, category=track.category, ts=now,
                                  dwell_s=track.dwell_s(now)) for n in names]
        return max(results, key=lambda r: r["deviation"])

    # ---- persistence hooks (store.py owns the file; see handoff) ----
    def record_sim_reset(self, ts: float | None = None) -> dict:
        """Note a sim_reset. Baselines are NOT cleared (M12)."""
        ts = time.time() if ts is None else ts
        self.sim_resets.append(ts)
        return {"sim_resets": len(self.sim_resets), "pois_retained": len(self._pois),
                "at": ts}

    def to_dict(self) -> dict:
        return {"version": 1, "min_samples": self.min_samples,
                "sim_resets": list(self.sim_resets),
                "pois": [b.to_dict() for b in self._pois.values()]}

    def load_state(self, state: dict) -> int:
        for row in state.get("pois", []):
            try:
                b = PoiBaseline.from_dict(row)
            except (KeyError, TypeError, ValueError):
                continue
            self._pois[b.poi] = b
        self.sim_resets = list(state.get("sim_resets") or self.sim_resets)
        return len(self._pois)

    @classmethod
    def from_dict(cls, state: dict) -> "PatternOfLife":
        pol = cls(min_samples=int(state.get("min_samples", 24)))
        pol.load_state(state)
        return pol


# --------------------------------------------------------------------------
# §4.7 — SALUTE / INTREP structured artifacts (M8)
# --------------------------------------------------------------------------
_ACTIVITY_CODES = {
    "stationary": "static in place",
    "emplaced": "emplaced in a prepared position",
    "on_march": "moving on a route",
    "manoeuvring": "manoeuvring off-route",
    "observed": "observed, motion not yet resolved",
}


def _activity(track: Track, now: float | None = None) -> dict:
    """SALUTE 'Activity' from measured kinematics + OB mobility (M8)."""
    ob = track.ob
    now = track.last_seen if now is None else now
    dwell = track.dwell_s(now)
    if ob.mobility == "fixed":
        # Test the platform BEFORE the measured speed, exactly as
        # threat.indicator_posture does. A bridge or bunker cannot manoeuvre,
        # so any apparent velocity is association noise, not movement —
        # otherwise SALUTE 'Activity' and the intent posture indicator
        # disagreed about the same contact.
        code, basis = "stationary", (
            f"{ob.mobility} installation — cannot reposition"
            + (f"; apparent {track.speed_mps} m/s is association noise"
               if track.speed_mps and track.speed_mps >= 0.5 else ""))
    elif track.speed_mps is None:
        code, basis = "observed", "single fix — no velocity resolved yet"
    elif track.speed_mps < 0.5:
        if dwell > 600.0 and ob.engages_air:
            code, basis = "emplaced", f"stationary {dwell:.0f} s in one position"
        else:
            code, basis = "stationary", f"speed {track.speed_mps} m/s over the last fix pair"
    elif ob.mobility_speed_mps and track.speed_mps >= 0.6 * ob.mobility_speed_mps:
        code = "on_march"
        basis = (f"{track.speed_mps} m/s is >=60% of the {ob.mobility} road speed "
                 f"({ob.mobility_speed_mps} m/s)")
    else:
        code = "manoeuvring"
        basis = f"{track.speed_mps} m/s, below the {ob.mobility} road speed"
    return {
        "code": code, "text": _ACTIVITY_CODES.get(code, code),
        "speed_mps": track.speed_mps, "heading_deg": track.heading_deg,
        "dwell_s": round(dwell, 1), "basis": basis,
    }


def _element_size(track: Track, peers: list[Track] | None) -> dict:
    """SALUTE 'Size': how many like contacts are co-located (M8).

    Counts same-category tracks within ELEMENT_RADIUS_M, so a convoy reports as
    one element of N rather than N separate 'size 1' contacts.
    """
    ob = track.ob
    members = [track.track_id]
    if peers:
        members += [p.track_id for p in peers
                    if p.track_id != track.track_id and p.category == track.category
                    and _ground_m(track.lat, track.lon, p.lat, p.lon) <= ELEMENT_RADIUS_M]
    n = len(members)
    nominal = max(1, ob.typical_unit_count)
    if n >= nominal:
        element = ob.typical_unit_size
    elif n == 1:
        element = "single platform"
    else:
        element = f"partial element ({n} of a nominal {nominal})"
    return {
        "count": n, "element": element,
        "text": f"{n} x {ob.name}",
        "members": members,
        "basis": (f"{n} same-category track(s) within {ELEMENT_RADIUS_M:.0f} m; "
                  f"nominal unit is {ob.typical_unit_size}"),
    }


def salute_report(track: Track, observer: str = "UAV",
                  peers: list[Track] | None = None,
                  now: float | None = None) -> dict:
    """Structured SALUTE report for one track (M8, PLAN §4.7).

    Every one of the six fields is filled from evidence; nothing is free text.
    'Location' is always the contact's own geo_point — never the observer's.
    """
    now = time.time() if now is None else now
    ob = track.ob
    conf = assess_confidence(track, now)
    best = track.best_observation()
    return {
        "format": "SALUTE",
        "track_id": track.track_id,
        "uid": track.uid,
        "size": _element_size(track, peers),
        "activity": _activity(track, now),
        "location": {
            "lat": round(track.lat, 6), "lon": round(track.lon, 6),
            "alt_m": round(track.alt_m, 1),
            "source": "contact detection geo_point",
            "observer_position_used": False,
            "fix_time": int(track.last_seen),
            "slant_range_m": (round(best.slant_range_m, 1)
                              if best and best.slant_range_m is not None else None),
        },
        "unit": {
            "category": track.category, "ob_class": ob.key,
            "assessment": ob.typical_unit_size, "role": ob.role,
            "text": f"{ob.name} — {ob.typical_unit_size}",
            "confidence": conf["level"],
        },
        "time": {
            "epoch": int(track.last_seen),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(track.last_seen)),
            "first_seen": int(track.first_seen),
            "last_seen": int(track.last_seen),
            "age_s": round(track.staleness_s(now), 1),
        },
        "equipment": {
            "platform": ob.name, "detected_as": track.name,
            "capabilities": list(ob.capabilities),
            "weapon_range_m": ob.weapon_range_m,
            "weapon_ceiling_m": ob.weapon_ceiling_m,
            "acquisition_range_m": ob.acquisition_range_m,
            "mobility": ob.mobility,
            "signature_cues": list(ob.signature_cues),
            "text": f"{ob.name} ({track.name})",
        },
        "confidence": conf,
        # flat convenience mirrors for roster/HUD consumers
        "category": track.category,
        "ob_class": ob.key,
        "confidence_level": conf["level"],
        "equipment_name": track.name,
        "lat": round(track.lat, 6),
        "lon": round(track.lon, 6),
        "observer": observer,
        "sightings": track.sightings,
        "speed_mps": track.speed_mps,
        "heading_deg": track.heading_deg,
    }


#: Default number of contacts an INTREP expands. See `intrep_report`.
SUMMARY_TOP_N = 10

DETAIL_LEVELS = ("summary", "full")

#: Ranks which contacts a capped INTREP expands first.
_CONFIDENCE_RANK = {"confirmed": 3, "probable": 2, "possible": 1}

#: The compact row for contacts past `top_n` — enough to name and re-request
#: one, never enough to be mistaken for the contact's full reporting.
_SALUTE_ROW_KEYS = ("track_id", "category", "ob_class", "confidence_level",
                    "equipment_name", "lat", "lon")


def _summarize_salute(contact: dict) -> dict:
    """Drop the nested confidence sub-object and its cited evidence.

    `confidence_level` and `confidence_score` survive as flat fields, so a
    reader still knows how far to trust the contact — only the derivation goes.
    """
    out = {k: v for k, v in contact.items() if k != "confidence"}
    conf = contact.get("confidence") or {}
    if "score" in conf:
        out["confidence_score"] = conf["score"]
    return out


def _salute_row(contact: dict) -> dict:
    return {k: contact[k] for k in _SALUTE_ROW_KEYS if k in contact}


def _derive_gaps(contacts: list[dict], coverage: dict, loal_events: list[dict],
                 tracks: list[Track], now: float) -> list[dict]:
    """Collection gaps the report can prove from its own contents (M8)."""
    gaps: list[dict] = []
    pct = coverage.get("coverage_pct")
    if pct is None:
        gaps.append({"type": "coverage_unreported",
                     "description": "no coverage figure was supplied by the mission planner",
                     "impact": "area completeness cannot be asserted"})
    elif pct < 100.0:
        gaps.append({"type": "area_not_covered",
                     "description": f"{100.0 - float(pct):.1f}% of the tasked area was not imaged",
                     "impact": "contacts may exist in the uncovered fraction"})
    unclassified = [c["track_id"] for c in contacts if c["ob_class"] == "unclassified"]
    if unclassified:
        gaps.append({"type": "unidentified_contacts",
                     "description": f"{len(unclassified)} contact(s) could not be matched "
                                    "to an order-of-battle class",
                     "impact": "no capability assessment possible for these contacts",
                     "track_ids": unclassified})
    weak = [c["track_id"] for c in contacts if c["confidence_level"] == "possible"]
    if weak:
        gaps.append({"type": "low_confidence_contacts",
                     "description": f"{len(weak)} contact(s) rest on evidence supporting "
                                    "only 'possible'",
                     "impact": "re-look required before these drive any decision",
                     "track_ids": weak})
    stale = [t.track_id for t in tracks if t.staleness_s(now) > 900.0]
    if stale:
        gaps.append({"type": "custody_lapsed",
                     "description": f"{len(stale)} track(s) not refixed in over 15 min",
                     "impact": "positions are extrapolated, not observed",
                     "track_ids": stale})
    for ev in loal_events or []:
        gaps.append({"type": "link_outage",
                     "description": f"lost/degraded link for {ev.get('duration_s', '?')} s "
                                    f"on {ev.get('vehicle', 'unknown')}",
                     "impact": "no collection during the outage window"})
    return gaps


def intrep_report(tracks: list[Track], *, mission_id: str | None = None,
                  mission_summary: dict | None = None,
                  coverage: dict | None = None,
                  sensor_conditions: dict | None = None,
                  loal_events: list[dict] | None = None,
                  gaps: list[dict] | None = None,
                  since: float | None = None, observer: str = "UAV",
                  now: float | None = None,
                  pattern_of_life: PatternOfLife | None = None,
                  detail: str = "summary",
                  top_n: int | None = SUMMARY_TOP_N) -> dict:
    """Full INTREP per PLAN §4.7 (M8).

    Six required sections: mission summary, coverage %, tracks with ids,
    sensor conditions, LOAL events, gaps. Every section is a structured slot
    the harness fills field-by-field; the server derives what it can prove
    (contacts, confidence roll-up, and the gaps implied by the other sections).

    SIZE: this is the report a harness actually reads, and it grows with the
    track store, which persists across runs — measured at 411 KB for 36 tracks.
    `detail="summary"` (default) carries every SALUTE field but drops each
    contact's nested `confidence` sub-object with its cited evidence, keeping
    `confidence_level`/`confidence_score`. `detail="full"` restores it. `top_n`
    caps how many contacts are expanded (None for no cap); the remainder are
    listed compactly in `contacts_omitted` and named in `truncation` — the
    roll-up counts (`by_category`, `by_confidence`, `gaps`) are always computed
    over EVERY selected contact, never only the expanded ones.
    """
    if detail not in DETAIL_LEVELS:
        raise ValueError(
            f"detail={detail!r} is not one of {DETAIL_LEVELS}; refusing rather "
            "than guessing, because the wrong guess silently changes what an "
            "ISR report contains")
    if top_n is not None and top_n < 0:
        raise ValueError(f"top_n={top_n} must be >= 0 or None (no cap)")
    now = time.time() if now is None else now
    sel = [t for t in tracks if since is None or t.last_seen >= since]
    contacts = [salute_report(t, observer=observer, peers=sel, now=now) for t in sel]

    by_cat: dict[str, int] = {}
    by_conf: dict[str, int] = {lvl: 0 for lvl in CONFIDENCE_LEVELS}
    for t, c in zip(sel, contacts):
        by_cat[t.category] = by_cat.get(t.category, 0) + 1
        by_conf[c["confidence_level"]] = by_conf.get(c["confidence_level"], 0) + 1

    summary = {
        "mission_id": mission_id, "kind": None, "vehicle": None,
        "started": None, "ended": None, "duration_s": None,
        "area_name": None, "status": "reported",
        "narrative": None,
    }
    summary.update(mission_summary or {})
    cov = {"planned_area_km2": None, "covered_area_km2": None,
           "coverage_pct": None, "method": None}
    cov.update(coverage or {})
    sensors = {"light": None, "weather": None, "visibility_km": None,
               "wind_mps": None, "sensors_used": [], "gps_quality": None}
    sensors.update(sensor_conditions or {})
    if not sensors["sensors_used"]:
        used = sorted({o.sensor for t in sel for o in t.observations})
        sensors["sensors_used"] = used
    loal = list(loal_events or [])

    pol_section = None
    if pattern_of_life is not None:
        pol_section = {
            "pois": pattern_of_life.pois(),
            "deviations": [
                {"track_id": t.track_id,
                 **{k: v for k, v in pattern_of_life.deviation_for_track(t, now).items()
                    if k != "evidence"}}
                for t in sel
            ],
        }

    # Rank so a capped report expands the contacts that matter most, not
    # whichever happened to be detected first.
    order = sorted(range(len(contacts)),
                   key=lambda i: (_CONFIDENCE_RANK.get(contacts[i]["confidence_level"], 0),
                                  sel[i].sightings),
                   reverse=True)
    ranked = [contacts[i] for i in order]
    shown = ranked if top_n is None else ranked[:top_n]
    rest = [] if top_n is None else ranked[top_n:]
    body = shown if detail == "full" else [_summarize_salute(c) for c in shown]

    report = {
        "format": "INTREP",
        "mission_id": mission_id,
        "as_of": int(now),
        "as_of_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "mission_summary": summary,
        "coverage": cov,
        "contacts": body,
        "sensor_conditions": sensors,
        "loal_events": loal,
        # Gaps are derived over EVERY selected contact, never only the expanded
        # ones - a truncated report must still report the whole picture's gaps.
        "gaps": _derive_gaps(contacts, cov, loal, sel, now) + list(gaps or []),
        "confidence_summary": by_conf,
        "pattern_of_life": pol_section,
        # ---- what this report expanded, and what it did not (never silent) ----
        "detail": detail,
        "detailed_count": len(body),
        "contacts_omitted_count": len(rest),
        "contacts_omitted": [_salute_row(c) for c in rest],
        "truncation": (
            None if not rest else
            f"{len(rest)} of {len(contacts)} contacts are listed in "
            f"'contacts_omitted' as id/class/confidence rows only. The counts, "
            f"confidence summary and gaps above cover ALL {len(contacts)}. "
            f"Raise top_n to expand more."),
        "detail_note": (
            "summary: full SALUTE fields, minus each contact's nested "
            "'confidence' sub-object and its cited evidence (confidence_level "
            "and confidence_score are kept). Pass detail='full' for those."
            if detail == "summary" else "full: every SALUTE field and its evidence."),
        # roll-up mirrors (kept for the existing tool contract)
        "total_tracks": len(sel),
        "by_category": by_cat,
        "new_contacts": [t.track_id for t in sel if t.sightings <= 2],
        # Deprecated mirror of `contacts`, kept for existing consumers.
        "tracks": body,
    }
    return report


def intrep_summary(tracks: list[Track], since: float | None = None, **kwargs) -> dict:
    """INTREP roll-up (M8). Thin alias of `intrep_report` kept for the existing
    `uav_target_report` tool signature; returns the full §4.7 template."""
    return intrep_report(tracks, since=since, **kwargs)
