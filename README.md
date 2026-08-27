# Minima

A go/no-go + best-window planner that gates every proposed flight against
**your own personal minimums**. Defaults are tuned for a VFR pilot based at
**CYFD (Brantford Municipal, ON)** in a Cessna 172-class aircraft, but your
home base, minimums, standing factors and risk tolerance are all editable and
travel with you as your flying evolves. It answers one question through the lens
of a **Personal Flight Decision Card**:

> **Am I good to fly - and when, over the next 24–48 h, is the best window to go?**

### Three ways to use it
1. **My Minimums** *(your profile, opens here)* - set your **home base** and your
   personal wind / ceiling / visibility / crosswind limits (drag sliders), the
   weather hazards that force a NO-GO, your **standing factors** (e.g. single-pilot
   no-autopilot), and a **conservatism** preset. Everything is stored in your
   browser, persists between sessions, and gates the Route and Discovery results.
2. **Route** - enter **departure + destination** (autocomplete over every Canadian
   aerodrome + US border fields; departure defaults to your base) and an **ETD in
   Zulu**. You get the **GO / MITIGATE / NO-GO** verdict *for the window you're
   actually flying* - each end shown at its own time, but gated on the worst
   conditions anywhere between wheels-up and wheels-down - plus flight time and
   best cruise altitude (winds aloft), the **en-route
   aerodromes** within 5 nm of your track, active NOTAMs/SIGMETs, and an
   **hour-by-hour 24–48 h timeline**, in Zulu like everything else, that
   highlights the best GO window(s).
3. **Discovery** - "where can I go within X nm of my base," ranked by the card.
   It takes an ETD too: each candidate is assessed at *its own* ETA, so a 20 nm
   hop and a 200 nm leg from the same departure time are judged on the weather
   each will actually meet. Legs past **50 nm** also get cloud and visibility
   sampled **along the way** - one midpoint past 50 nm, two past 100, three past
   150 - each read at the hour you overfly it, and each able to fail the card.
   Every card says how many points it took, so *"clear enroute"* and *"enroute not
   checked"* never look alike. Under 50 nm the two ends are the route, and the
   card says that instead. The whole scan's midpoints ride one extra batched
   request.

### Departure time (ETD)
The picker offers **quarter hours for the first four hours**, then whole hours out
to the 48 h forecast horizon, and every option carries both the Zulu time and how
far off it is - `Today 1445Z · +20 min`. Departures are planned in quarter hours,
and an hourly-only list had nothing to offer a flight leaving in twenty minutes.

Leaving the ETD on **Now** keeps the METAR as the anchor for departure conditions,
because an observation is the best statement about the next half hour - which is
also how far the "Now" grace extends, so `+30` and beyond genuinely switch to the
forecast rather than silently returning the same answer. Pick a *future* ETD and
the endpoints run on the forecast, with the **TAF taking precedence over HRDPS**
on everything it actually states - ceiling, visibility, wind and hazards. A TAF
wind carries its gust with it, including the *absence* of one: where the TAF
forecasts a steady 10 kt, a modelled 30 kt gust does not survive into the card.
HRDPS fills only the gaps the TAF leaves. Every value stays labelled with where it
came from, per field.

The ETD is not remembered across reloads (a restored "yesterday 14:00Z" would
silently assess the wrong flight), and if the time you picked passes while the tab
sits open the control resets to **Now** and says so rather than changing quietly
underneath you.

### Would waiting help?
Directly under the go/no-go, two different questions get answered.

If the flight is **not** a GO, the nudge says how far you are from one - the
nearest hour whose forecast turns MITIGATE or GO, provided the good spell is long
enough to hold your whole leg, and what stops applying when you get there.

If the flight **is** already a GO, you still get told when waiting would buy you
something real: **route wind at cruise**, **ceiling**, a **gating hazard clearing**,
or **crosswind**. The wind figure is the component along your course at the
altitude that hour's winds and that hour's ceiling would actually support, not the
surface wind - a tailwind that only exists above a deck you cannot legally climb
through is not a reason to wait for it.

Two rules keep this from becoming noise. An option must be **no worse on every
axis it does not improve**, so a 25 kt wind swing that comes with a deck 3,500 ft
lower is never offered. And the thresholds are yours (*My Minimums*): 10 kt of
wind, 1,500 ft of ceiling, 5 kt of crosswind by default, searching up to 12 h
ahead. Most flights produce nothing here, which is the correct answer on most
flights. It is **advisory only** and never appears as a reason not to go now.

### The observation horizon
An observation describes the next half hour, not the next afternoon. Once the ETD
is **3 h or more** out, the current METAR, the METAR history and the trends drawn
from it are dropped from the cards entirely - the forecast is the only thing
gating that flight, and a three-hour-old ceiling invites anchoring on conditions
that will not exist at departure. They are not merely hidden but never fetched,
which also takes the flakiest upstream out of the request.

One consequence worth knowing: inside the horizon, "lowering ceilings" can be
raised either by the model trend *or* by what the last few METARs actually did.
Past it, only the model trend can raise it.

When the observation-history service is asked and doesn't answer, the card now
says so. It used to render exactly the same empty space as "nothing is trending",
which is why trends could appear on one run of a route and vanish on the next.

**A field that publishes no METAR is never asked for its history, and never told
that history failed.** The request could only ever come back empty, so blaming
the download for the absence puts a line in the banner that can never mean
anything - and a banner that cries wolf is one the pilot learns to click past.
The ident is no guide here: `CYFD` looks like a reporting station and is not, so
the test is whether a METAR actually came back.

### The flight window
Everything is assessed for the span you are actually airborne - **ETD→ETA, ±30 min**
for taxi and approach - not for a single instant:

* **Hazards** are read from the TAF segments overlapping that window, not grepped
  out of the whole forecast. This is what stops a thunderstorm forecast for
  tomorrow evening from grounding a flight at noon today. A storm outside your
  window still appears, as an advisory row naming the period it applies to.
* **Ceiling, visibility and wind** are the worst case anywhere in the window. A
  `BECMG` is a permanent change, so it governs from the start of its transition;
  a `TEMPO` you fly through counts at its worst. Gating on the ETD instant used to
  pass a flight that flew straight into a 2 SM / 800 ft TEMPO twenty minutes later.
* **A `TEMPO` on its own asks for an out; a sustained group stops the flight.** A
  `FM`/`BECMG` below your minimum is the forecaster saying the weather *will* be
  below it for a sustained stretch - that is a NO-GO. A `TEMPO` is the same
  forecaster saying conditions will be predominantly better with temporary
  deteriorations, and the honest answer to that is not "go" but *go with an out*:
  fuel, an alternate, a decision point. So a bust traceable only to a `TEMPO` is
  **MITIGATE**, not NO-GO. The row still fails and still names the group. "Only"
  is load-bearing: if the sustained forecast underneath is *also* below your
  minimum, the flight is below minimums with or without the `TEMPO` and it stays
  a NO-GO, however much deeper the `TEMPO` happens to go.
* On a **Now** departure the METAR still owns the departure-instant values - a
  forecast never overrides an observation of the present moment - and what the TAF
  says about the rest of the leg appears as its own clearly-labelled rows, naming
  the group it came from (`Ceiling in flight window (TEMPO 1900Z-2000Z)`).
* **PROB30/PROB40** never fail the card on ceiling, visibility or wind. The row
  keeps the TAF's own language: `PROB30 1800Z-2300Z | Advisory only | wind 20G30
  kt, 4,000 ft ceiling, 3 SM visibility, thunderstorm`.
  A **hazard** carried only by a PROB group (a `PROB30 TSRA`) is different: it
  gates only if you have that hazard on your **Weather auto NO-GO** list in
  Settings, and otherwise shows as an advisory naming the group. Thunderstorm is
  on that list by default, so a PROB30 TSRA is still a NO-GO out of the box -
  turn it off and it becomes a caution instead.
* **Embedded convective cloud** is the one hazard an instrument rating does not
  answer, so it is an automatic NO-GO on an IFR card as well as a VFR one -
  unlike *Widespread IMC*, which is a VFR row and is not built on an IFR card at
  all. It is read off all three of the spellings NAV CANADA uses (`EMBD TS`,
  `EMBD CB`, `CVCTV CLD EMBD`) wherever they appear - a METAR remark, a TAF
  group, a SIGMET or an area forecast - and like every hazard it is time-scoped:
  a TAF group gates only when it overlaps your window, and an observation gates
  a **Now** departure and drops to an advisory for a later one. Untick it in
  Settings and it becomes a caution instead.
* That rule lives in **one place** and every view reads it: the route card, the
  discovery cards and the hour-by-hour strip. They used to disagree - the strip
  folded PROB groups straight into the hour it was grading, so a `PROB30 2SM`
  turned a cell red that the route card called advisory, while discovery ignored
  PROB hazards altogether. In the strip a PROB hour now carries an **amber edge**
  and names the group in its detail panel, the same signal the TAF strip uses.

### Aerodromes with no TAF of their own
When a field doesn't report, the nearest reporting station's METAR/TAF is shown -
split into periods and highlighted like any other, because it is the only forecast
text available. It is **reference only and never gates**: a TAF's ceiling,
visibility and wind describe roughly a 5 SM radius around *its* aerodrome, and
CYFD to CYHM is 13 nm of escarpment and lake-breeze. Regional hazards reach the
verdict through the GFA, SIGMETs and AIRMETs, which are actual area products
covering your field, rather than by borrowing a neighbour's aerodrome forecast.

### GFA charts
The GFA panel opens on the chart that **covers your ETD**, not the current one -
at 1900Z with a 0100Z departure you get the 0000Z panel, not the 1800Z one. The
covering panel stays marked (✈) after you click to another, and if your ETD is
past the reach of the latest issuance the caption says so rather than showing a
chart that doesn't describe your flight.

### SIGMETs, AIRMETs and PIREPs
Two independent, free, no-key feeds, fetched together and merged:

* **NAV CANADA CFPS** — the authoritative Canadian source, queried for the route's
  aerodromes *and* the FIRs it flies through, since SIGMETs and AIRMETs are issued
  per FIR. Which FIRs those are comes from `services/firs.py`, a coarse box per
  region (`CZVR CZEG CZWG CZYZ CZUL CZQM CZQX`) drawn generously enough that a
  flight near a boundary asks about both neighbours; a route the boxes cannot
  place falls back to all seven. This used to ask about all seven regardless,
  which is how a pilot flying circuits in Ontario got an Edmonton AIRMET: a
  bulletin whose area is written in words rather than coordinates has no polygon
  to test, so it fails open, and the only reliable way not to show it is not to
  fetch it.
* **aviationweather.gov (NOAA/AWC)** — international SIGMETs (which cover the
  Canadian FIRs too, so the two feeds cross-check each other), plus the US
  domestic SIGMET/AIRMET, G-AIRMET, CWA and PIREP products a cross-border leg
  needs and CFPS knows nothing about. Requested as **GeoJSON**, so each advisory
  arrives with the polygon the forecaster drew.

The same product reaching us from both sources is **merged, not deduplicated
away**: NAV CANADA's wording is authoritative, AWC is the one carrying a polygon,
and both halves are kept.

**Relevance is geometric.** The route is sampled into ≤25 nm legs and each
advisory's area is tested against that line — inside it, crossing it, or within a
25 nm corridor of it. The old test measured the distance from three route points
to each polygon *vertex*, which dropped a SIGMET big enough to contain the whole
flight and admitted one the route never entered. On top of geometry, the altitude
band must overlap your slab (surface to cruise + 2,000 ft) and the validity window
must overlap ETD→ETA. Every test **fails open**: an advisory whose position, band
or validity can't be parsed is kept and shown.

One exception, and it is the only way a bulletin is set aside on something other
than its own shape: an advisory with **no polygon at all** that names a **FIR the
flight never enters** is dropped. With no geometry, the region is the only
evidence of where it is, and without this rule a Reykjavik advisory rides along
with every flight. The test used to apply to the aviationweather.gov feeds only,
which left out CFPS — the one feed queried per region, and the one whose
bulletins most often describe their area in prose.

Only a **SIGMET or CWA that passes all three** moves the verdict, and the reason
names it (`CZYZ SIGMET A1 SFC-FL180 on your route`). AIRMETs and G-AIRMETs reach
the verdict through the icing and turbulence rows below instead, which grade
severity against the altitudes actually flown. Any SIGMET anywhere used to force
MITIGATE — survivable with one product, meaningless with seven.

**PIREPs never gate.** A SIGMET or AIRMET is a forecaster's statement about the
airspace; a PIREP is what one aeroplane met at one moment, usually not an
aeroplane like yours. They are read (including the coded `/TB` and `/IC` fields,
where the word "turbulence" never appears) and reported on the icing and
turbulence rows as advisories, but a single airliner's "MOD turb" in the climb
does not cancel your flight.

**A PIREP has to say where and when, or it isn't shown.** A SIGMET is a shape you
can be twenty miles outside of and still want to see the edge of; a PIREP is one
aircraft at one point, so how far off track it was is the whole of what it is.
Anything beyond **50 nm** of the route is dropped outright rather than listed
faint, and so is one whose `/OV` field can't be placed at all - with no position
there is nothing to draw and no distance to judge it by, and such reports were
riding onto the card marked relevant from anywhere in the country. Placing them
needs US aerodromes and navaids, which the Canada-only airport table does not
carry, so positions resolve against their own wider table. Each PIREP also shows
**how old it is**, the way a METAR does: green under the hour, red over it. A
PIREP describes air one aeroplane flew through, and after an hour that air has
moved.

Advisories that downloaded but don't apply to *you* are **shown, not discarded**,
under a line reading *"4 more fetched: 3 outside your altitudes, 1 not on your
route"* — "we found four and none reach you" and "we found none" are different
statements, and only one of them is good news. Anything more than 150 nm off
track is dropped outright rather than counted: the feeds are national, and
telling a pilot in southern Ontario that 268 advisories over the prairies don't
apply is noise, not honesty. Everything kept is also drawn on the route map:
solid polygons for the ones that apply, dashed and faint for the near misses,
PIREPs as points, each with its full text on tap.

### Flight category
The same map carries a **flight category** layer: every aerodrome reporting a
METAR near the route, drawn as one dot in the standard scheme - **green VFR,
blue marginal VFR, red IFR, purple low IFR**. Ceiling and visibility otherwise
exist in this app as two numbers on two endpoint cards, which answers what it is
like where you are leaving from and nothing else. The question you actually ask a
weather map first is *where is the good air and where is the bad*, and that is a
shape - the edge of a marginal area north of track, and which way it is leaning -
not a pair of readings.

The thresholds are the usual ones, with the **worse of the two axes winning**, so
a 5,000 ft ceiling never makes 2 sm of visibility flyable:

| | Ceiling | Visibility |
|---|---|---|
| **VFR** | above 3,000 ft | more than 5 sm |
| **Marginal VFR** | 1,000-3,000 ft | 3-5 sm |
| **IFR** | 500 to under 1,000 ft | 1 to under 3 sm |
| **Low IFR** | below 500 ft | below 1 sm |

**No ceiling reported is unlimited, not unknown.** `SKC`, `CLR`, or nothing above
a `SCT` layer means there is no ceiling because there is no ceiling - a positive
statement about the sky - so the category falls to visibility alone. Only `BKN`,
`OVC` and `VV` make a ceiling, which is why `SCT002 OVC008` is an eight-hundred
foot field and not a two-hundred foot one.

**A report that can't be read is grey, not absent.** A station dropped for being
unparseable leaves empty map, and empty map reads as *nothing here* when the
truth is *something here we couldn't decode*. The only station dropped outright
is one that can't be placed at all - a dot with no position is not a dot. And a
report **older than 90 minutes is drawn faded**: an observation describes the
half hour around it, and at two hours old it is describing air that has moved on.

**How far out it reaches: 150 nm past the route's own bounding box** - the same
distance the advisories already use for "far enough off track to still be worth
showing you". It is also the right number to look at. The map opens framed on
your route, which is two to four hundred miles across, so a 150 nm pad fills the
view and a little beyond rather than stopping in a hard rectangle edge
mid-screen. The advisories' 25 nm corridor would draw a narrow ribbon of dots on
an otherwise empty map, which loses the point entirely: you read a category map
to see the *edge* of the bad air, not the six dots on your own track. It pads a
rectangle rather than a corridor for the same reason - a corridor renders with
visibly shaved corners, and a missing dot reads as "no station" instead of "not
fetched". Each dot still says how far off track it is when you tap it.

**They are observations, not a forecast, and the legend says so.** Everything
else on the card is assessed for the window you actually fly; these dots are the
sky right now, because that is what a METAR is. On a flight leaving in three
hours those are two different statements, so the legend prints *"observations,
not a forecast - newest 1800Z - your ETD is +3 h"* rather than leaving you to
assume one or the other.

**Nothing here gates anything.** The verdict is still the evaluator's, run
against *your* minimums at *your* ETD. A flight category is a national
convention with fixed thresholds and knows nothing about you: a 1,500 ft
marginal-VFR ceiling is an ordinary day for one pilot and a hard NO-GO for
another. This layer is a picture, and only a picture.

The toggle sits in the map's layer control with the advisories, is **on by
default**, and remembers being turned off. The stations load after the map is
already drawn - the map does not wait for them - and if the feed is down you
lose the dots and keep the radar, the hazards and the course line.

**Circuits get the same picture, scoped to one point.** The circuits card used to
print *"Weather (TAF + SIGMET/AIRMET/PIREP + model)"* over a card that fetched
none of the three, so a SIGMET sitting over the field rendered as the same empty
space as a clear sky. It now fetches all seven products, tests them against the
aerodrome instead of a track, and shows them in the same advisories panel and on
the same radar map - one marker, no course line. The altitude slab is **surface
to 3,000 ft above the field** rather than the route's cruise + 2,000: a circuit
sits at 1,000 ft AGL, and a SIGMET at FL240 has nothing to say to a flight that
never leaves the aerodrome. One Weather row carries the result, and a relevant
SIGMET or CWA fails it; AIRMETs and PIREPs are reported without gating, the same
standing they have on the route card.

### Icing and turbulence
Nothing can parse a GFA chart, so these two rows used to read *"review the GFA
icing chart"* with an amber ⚠ - on every flight, in every season, whatever the
weather, next to a link that went to the NAV CANADA front door rather than to the
chart embedded further down the same page. A warning that fires every time teaches
you to ignore warnings. Both rows now come from two real sources:

* **Area products** (AIRMET / SIGMET / PIREP) are read for **severity and altitude
  band**, so only a *moderate-or-worse* report overlapping the altitudes you'll
  actually occupy - surface to cruise plus 2,000 ft - stops the flight. A light-chop
  PIREP no longer grounds you, an FL240–FL400 turbulence SIGMET no longer applies to
  a Cessna at 4,500 ft, and `ICE PELLETS` / `ICE CRYSTALS` / `NO ICE` are no longer
  read as airframe icing.
* **The HRDPS model** already being fetched for wind and cloud carries per-level
  temperature and humidity, so the app finds the layers where cloud sits between
  0 °C and −20 °C and reports them as altitude bands ("cloud below freezing
  3,500–7,800 ft at −3 to −11 °C, freezing level 3,100 ft"). Turbulence comes from
  vector wind shear through the low levels, the surface gust factor and the 925 hPa
  low-level jet.

The model half is **advisory and never gates** - a model is not a forecaster, and
this one has no terrain-wave, convective or frontal reasoning in it. What it does
is replace "go and read a chart" with a description of the air plus *confirm on the
GFA panel below*, and stay silent on the days there is nothing to say.

### Density altitude
The one performance limiter none of the other rows can see. A day comfortably
inside every wind, ceiling and visibility minimum can still cost a normally
aspirated trainer a large slice of its climb rate and stretch its takeoff roll well
past the POH numbers - and nothing about the wind or the cloud tells you that:

```
PA = field elevation + (29.92 − altimeter) × 1000
DA = PA + 120 × (OAT − ISA temperature at that PA)
```

NAV CANADA broadcasts density altitude on ATIS/AWOS once it exceeds aerodrome
elevation by **200 ft**; this app is quieter, and advises at **500 ft** by default
(tunable in *My Minimums*, 0–5,000 ft). The row names the absolute density altitude
first - the number you take to the performance chart - then how far above field
elevation it is, then the temperature and ISA deviation that produced it, so you can
check it against the METAR printed on the same card.

It is **always and only an advisory**: amber, never red, never a threat, and
structurally incapable of moving your verdict - it's appended after the decision is
made. The go/no-go on a density altitude belongs to whoever has the aircraft's
numbers in front of them.

**Observation for now, forecast for later, and the row says which.** A METAR is
the right instrument for a departure in the next few minutes and the wrong one for
a departure at 1900Z - so a planned ETD is answered from the model instead, blended
across the five models where they cover the field. This used to be observation-only,
which meant the flight that most needs the row - a hot afternoon departure, hours
away, whose performance you cannot yet observe - got no row at all.

Two things the model path is careful about. Field elevation always comes from the
airport database, never from the model's grid-cell elevation, which can be out by
hundreds of feet - the same order as the thing being measured. The altimeter setting
comes from `pressure_msl` rather than `surface_pressure`, which is referenced to
that same grid cell; the model temperature is corrected from the grid cell to the
real field by the ISA lapse rate, because every degree is 120 ft of density altitude.

### En-route aerodromes
A collapsed-by-default list of every aerodrome within **5 nm of the straight
route**, in the order you'd fly over them, with runway, surface, length and the
modelled wind **at your overfly time**. Grass and private strips are included on
purpose: these are precautionary-landing options, not destinations. This section
is situational awareness only and never affects your verdict.

### The surface filter and the runway a card recommends

Filtering discovery to **Hard (paved)** keeps aerodromes that *have* a paved
runway - and many of the interesting ones also have a grass strip. The runway a
card headlines as "best runway into wind" is now picked from the surface you
asked for, not from every runway at the field.

That used to be a wind-only pick, and it was wrong in a way that reached past the
label. The headline runway is what the crosswind limit row is evaluated on, what
"Within my crosswind limit" filters on, and what the crosswind sort orders by. So
at a field whose grass strip lay across the paved one, a hard-paved scan would
recommend the grass, report its zero crosswind, and pass a crosswind filter the
paved runway - the only one you said you'd use - failed by 6 kt. Brantford
(CYFD), the default base, is exactly that field: asphalt 05/23 and turf 14/32, 90
degrees apart.

The runway dropdown still lists **every** usable end, including the ones that
don't match - a strip you can't use in a crosswind is still a strip, and worth
knowing about. Those rows are dimmed and tagged `not hard-paved` so they read as
what they are rather than as the recommendation. Aerodromes whose surface the
dataset doesn't recognise are never tagged: "we don't know" isn't "wrong for you".

### Why a discovery candidate isn't a GO
Every non-GO card says so on its face, directly under the badge: the personal
minimums it busts, the **stacked threats** with the count and what the stack comes
to (`2 threats → No-go solo`), and any advisory that did *not* count against it.
A card used to render only the failing limit rows - so a verdict that came from the
threat stack, which is most MITIGATEs, arrived with a red badge and no explanation
at all. A clean GO card stays silent.

### Reading a discovery card for a future ETD

A discovery card compresses a whole flight into one line - one wind, one ceiling,
one visibility, worst case anywhere across the leg. That is the right input to a
go/no-go and the wrong amount of information for planning a departure hours out,
where the question is which way it is *moving*. So two things go on the card:

- **Planned ETD beside every aerodrome name.** `Planned ETD 1800Z → ETA 1845Z`.
  Every candidate leaves at the ETD you picked and arrives at its own ETA, so a
  list of twenty aerodromes reads without scrolling back to the dropdown.
  Omitted on a **Now** scan, where the answer is "now".
- **The provenance chip opens.** The blue **TAF** and yellow **HRDPS** chips are
  buttons - hover on a mouse, tap on a phone. TAF opens the full forecast split
  into its FM/BECMG/TEMPO/PROB periods, with **the periods you fly through in
  green**, exactly as the route endpoint cards draw it. HRDPS opens the model
  hour by hour from three hours before your ETD to three hours after that
  candidate's ETA, with the airborne hours green. The HRDPS view is the raw
  model - no TAF overlay, no verdict - because that is what the chip claims to
  be showing. `Observed` chips stay plain: the card already prints that METAR in
  full underneath.

### Two-trigger threat stacking (general-audience)
The decision card stacks "major threats": some are derived automatically from the
forecast (actual IMC, convective, icing, strong/gusty winds, turbulence/shear),
night is set by the day/night toggle (and is opt-out, see below), **standing
factors** come from your saved
profile, and **unfamiliar / complex airspace** is a per-flight toggle (it's
pilot-relative, so it works at any airport). A **conservatism preset** sets how
readily a stack escalates the verdict:

**No aerodrome raises the airspace threat by itself.** Busy fields used to be
carried on a built-in list - Hamilton, City Centre, Pearson, Kitchener - and the
threat was stacked on every flight touching one. But whether airspace is
unfamiliar is a fact about the *pilot*, not the field: someone who flies into
Hamilton every other weekend is not the pilot that warning is written for, and
with one automatic weather threat alongside it (strong winds trips at 15 kt on
the defaults) that pairing read **NO-GO** on a flight they would happily make.
The list is gone. The tick under "This flight - extra threats" is the only thing
that raises it, at any aerodrome, and you are the one who knows.

| Preset | Behaviour |
|--------|-----------|
| **Standard** *(default)* | One threat → mitigate, two → no-go (the original card). |
| **Confident** | Tolerates one threat; two → mitigate, three → no-go. |
| **Cautious** | A single *serious* weather threat (IMC / convective / icing) is disqualifying. |

The automatic **strong / gusty winds** threat trips *below* your hard limit -
wind you can legally accept is still wind worth planning for. It is scaled off
your own wind minimums (`threat_stacking.auto_threat_fraction`), so raising a
limit raises the trigger with it: on the default 20 kt sustained / 10 kt gust
spread it fires at 15 kt and 8 kt, and a pilot who sets a 20 kt gust spread is
not flagged until 16 kt.

### Day or night, and whether night is a threat
The day/night toggle **selects itself from your flight**, using civil twilight:
night is the CARs 101.01 definition - from the end of evening civil twilight to
the beginning of morning civil twilight - not sunset to sunrise, and not the
one-hour-either-side currency window. It defaulted to Day on every load before, so
a 0200Z departure was quietly assessed against daytime ceiling and visibility
minimums unless you remembered to flip it.

**Both ends count.** A flight that leaves in daylight and lands after evening civil
twilight *is* a night flight, and the toggle decides which set of personal minimums
the whole assessment runs against - so answering from the departure alone handed
that arrival the day limits. The ETA is the great-circle distance over your own
cruise TAS. The toggle re-derives whenever the departure, the destination, the ETD
or the aircraft changes. Clicking either button still wins, until one of those
changes - which is a different flight.

The control itself is inert: the selected half moves and nothing else does. It
used to switch to a dashed border when it had chosen for you, and to carry a
caption whose length silently set the control's width, so choosing an ETD redrew
its outline and grew an empty box past the second button. Both are gone; the
arrival note that explained a night landing went with them, since the toggle
position is the answer and the assessment already spells the arrival out.

In the hour-by-hour strip, night is a property of **each hour**, not of the flight:
dark hours carry the night threat and night minimums, daylight hours don't,
whichever way the toggle is set. Selecting "Night flight" used to stack a night
threat on all 48 hours, including ones in full daylight.

Whether night **stacks a threat** on the decision card is yours to set, in
My Minimums → **Night operations**. It is on by default (the original card). For
some pilots night is the single biggest risk multiplier; for others, current and
over familiar terrain in stable VMC, it is a normal flight. Turning it off changes
only the threat stack - night still selects your **night** ceiling and visibility
minimums either way.

It also decides whether the **last-light note** appears on the summary strip:
`🌇 Last light 0142Z · 45 min of daylight left after landing`, with the latest ETD
that still lands inside it once the margin is under an hour. The margin is
measured to the moment you are on the ground, not to takeoff - which is what
"after landing" is there to say. A pilot who has turned the night threat off is
night-current and equipped, so for them the countdown is one more line to read
past on the day it matters, and it stays hidden. The arrival note that spells out
a night landing is separate and shows either way.

> **Still using built-in defaults:** the numeric thresholds that *derive* the
> automatic weather threats (e.g. wind ≥ 15 kt counts as "strong") are not yet
> separately editable, and the route-hazard scan uses the built-in hazard list.

> ⚠️ **Decision-support only.** Forecasts are not observations. Always confirm
> with an official NAV CANADA briefing before flight.

## Accuracy & data provenance

Every weather value is labelled with where it came from, and the app always
prefers real aviation data over the model:

| Layer | Source | Role |
|-------|--------|------|
| **Observed** | NAV CANADA CFPS **METAR** | Anchors "now" |
| **TAF** | NAV CANADA CFPS | Authoritative forecast hazards + categorical worsening (TS, FZRA, low IFR) |
| **HRDPS** | Open-Meteo **GEM/HRDPS** (Canada 2.5 km, hourly, 4×/day) | Numeric hour-by-hour backbone + fallback where a field has no METAR |
| NOTAM / SIGMET | NAV CANADA CFPS | Route hazards |
| Runways / aerodromes | OurAirports | Geometry + the practical free **CFS proxy** |

The timeline combines both endpoints conservatively (worse of the two) and runs
the decision card on each hour. Where a field has both METAR and model, the
model-vs-observed wind delta is shown as a confidence hint.

### How good is the en-route ceiling?
Honestly: it is a **planning aid, not an observation**, and worth understanding
because it is the weakest number on the page.

GEM does not serve a cloud base, so an en-route ceiling is *inferred* by scanning
pressure levels for the lowest broken-or-worse layer, taking each level's real
geopotential height for that hour and interpolating the base between levels. That
buys roughly ±700–1,000 ft. It is structurally blind to decks thinner than the
level spacing, and it cannot see above the top of the scan - so the card says
*"clear below ~17,000 ft AGL"* rather than "clear", because the latter claims more
than the method supports.

The same walk reports the **whole stack**, not just the layer that makes a
ceiling: `SCT ~2,400 ft · BKN ~5,300 ft`. That matters because "no ceiling" has
four meanings and only one of them is good news, so each gets its own sentence -
*not assessed* (the forecast did not download), *clear below ~X*, *SCT ~4,000 ft ·
no broken layer*, and the stack itself. They are never rendered the same way, and
none of them is ever rendered as an empty space.

Cloud **type** shows where it was observed and nowhere else: `CB`/`TCU` off the
body group, and the genus off the Canadian remarks (`RMK SC8` → `OVC 1,000 ft SC`,
`RMK CU6CI1` → cumulus under cirrus). No forecast model carries a cloud type, so a
model-derived layer shows none rather than guessing one.

The much better answer, where one exists, is a **real report**: the nearest
reporting station to each route midpoint rides the METAR/TAF batch the route
already issues, and is merged worst-of. An observed broken layer **lowers** the
route ceiling; an observed clear sky **never raises** it, because a field 30 nm
off track being clear is no evidence about the air over your course. Past the
observation horizon that station's TAF is read at the hour you are actually over
the point. Each row names the station and distance behind its number, so a real
observation is always distinguishable from an inference.

On the airport cards the TAF is split into its **FM/BECMG/TEMPO/PROB periods**,
and **every period the flight passes through is green** - whatever kind of group
it is - with the rest dimmed. So "what does the forecast say for my flight" is one
glance rather than a paragraph of parsing. Both endpoints mark the *same*
ETD→ETA span, so green means one thing wherever you read it; previously each card
marked only the group covering its own one relevant instant, which left a TEMPO in
the middle of the leg highlighted on neither while it still gated the verdict.
A PROB30/40 in the window is green too, with an amber edge to say it does not
gate. Where a field has no TAF of its own, the nearest reporting station's TAF is
split and highlighted the same way. The raw TAF is kept underneath to cross-check
against.

### When a fetch fails

Every upstream call degrades to an empty default rather than failing the whole
assessment - one dead service should not cost you the rest of the card. The
trap is that an empty default looks exactly like good news: a dropped HRDPS
request gave an empty hourly forecast, which gave an empty timeline, which the
route page printed as *"No clearly favourable window in the next 48 h"* - a
confident claim about weather nobody had downloaded. Pulling the data again
fixed it, which was the tell.

So the app now does three things about it:

1. **Retries once.** A single dropped connection or slow response is retried
   inside the upstream clients (`app/sources/_http.py`) before it counts as a
   failure at all.
2. **Reports what is missing.** Anything that still fails is recorded by product
   name (`app/services/fetch_health.py`) and rides back with the answer as
   `data_health`. The page draws a red **"Failed to fetch some data"** banner
   above the verdict, listing which products are missing, with a **Pull the data
   again** button.
3. **Refuses to state a finding it did not compute.** With no hourly forecast,
   the best-windows and hour-by-hour sections say the forecast did not download -
   explicitly *not* "no good window". A 500 or a dropped request on any of the
   three weather endpoints raises the same banner rather than an unexplained
   empty list.

Two upstreams stay deliberately quiet, because they degrade into a *smaller*
card rather than a wrong one: the multi-model wind blend (the single-model wind
is still there behind it), and the GFA/radar panels, which have carried their
own "couldn't be loaded" fallbacks since they were added.

Area advisories are judged the same way, by **what was lost rather than what
failed**. The two upstreams overlap on purpose, so AWC's PIREP feed dropping
while NAV CANADA's still answers costs nothing and raises nothing — a banner
every single time is how you teach someone to click past the banner on the day
it means something. The banner names a *kind* of advisory (`SIGMETs`,
`AIRMETs`, `PIREPs`) only once no source for it is left, with the individual
upstream and its HTTP status behind a **Which sources** fold for diagnosis.

> **Known gaps:** The TAF parser doesn't recognise `INTER`, so such a group's conditions are
> absorbed into the preceding one rather than windowed on their own; `CAVOK`, `NSW`
> and metric visibility are likewise unparsed.

### Why not Windy?
A Windy.com **Premium** subscription does **not** include API access - Windy's
Point Forecast API is a separate **Professional license (~$1,000/yr)** and its
free key returns deliberately degraded data. So Minima uses **Open-Meteo
HRDPS** instead: free, no key, and the highest-resolution hourly model available
for southern Ontario.

### What "CFS coverage" means
The full Canada Flight Supplement has no free API. "CFS coverage" here is the
**OurAirports Canadian aerodrome list** (runways/elevation), a practical free
proxy - not licensed CFS content. There's a seam to plug in a paid CFS feed later.

## How the decision card is applied

The card (`data/limits.yaml`, fully editable) drives everything:

- **Hard limits** → any breach = **NO-GO** with the specific reason: wind > 20 kt,
  gust spread > 10 kt, crosswind > 9 kt, XC ceiling < 4000 ft AGL (day) /
  cloud base < 12000 ft (night), XC visibility < 9 SM, and hazard flags. All of
  it is evaluated for your ETD→ETA window, not for "right now".
  A gust spread is judged against the *peak*: below `gust_spread_floor_kt`
  (15 kt) the spread is reported and does not gate, because a model's wind and
  its gust are different statistics and 10 kt of spread under a 13 kt peak is
  not the weather that limit was written for. That one is tuned in the YAML and
  has no slider - it is a correction, not a personal minimum.
- **Two-trigger threat stacking** → 0 = GO, 1 = MITIGATE, 2+ = NO-GO. Some threats
  are derived from the weather; others (night ops, fatigue, etc.) you tick in the UI.
- **Pilot fitness / external pressure / "explain it to your instructor"** → a
  self-assessment checklist that flags the whole session to pause and reassess.

## Run locally

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # open http://127.0.0.1:8000
pytest -q                              # offline logic tests (live tests auto-skip)
python scripts/probe_area_products.py  # what the advisory upstreams actually return
```

`probe_area_products.py` is the answer to *"is the SIGMET feed working?"*. NAV
CANADA's API is undocumented, so the script asks it for each area product in every
plausible request shape side by side and prints which ones answer and what they
return, then does the same for each aviationweather.gov product in JSON and
GeoJSON. Run it anywhere the network is open; `--save` writes the payloads into
`tests/fixtures/area/` so the offline suite parses real responses rather than
invented ones.

## Airport data

The full **all-Canada + US-border** dataset is baked into the Docker image at
build time, and is **auto-bootstrapped** from OurAirports on first launch
anywhere the network is open. Until then a bundled seed of ~28 common ON/QC/border
fields is used. To (re)build it manually:

```bash
python scripts/refresh_airport_data.py
```

> If hosting inside a sandbox with an egress allowlist, allow
> `plan.navcanada.ca`, `aviationweather.gov`, `api.open-meteo.com`, and (for the
> airport refresh) `davidmegginson.github.io`.

## Deploy on your own domain (installable PWA)

Minima ships as an installable **Progressive Web App** (manifest + service
worker + icons under `web/`); the service worker caches only the static shell,
never `/api/*`, so weather data always stays live. It's hosted on **Fly.io** at
`minima-wx.fly.dev` for ~$1-3/month, with an optional custom domain — see
**[DEPLOY.md](DEPLOY.md)** for the step-by-step guide. The repo includes `fly.toml`, a
`Dockerfile`, and a GitHub Action (`.github/workflows/fly-deploy.yml`) that
auto-deploys on every push to `main`.

### Why the first assessment of the day is the slow one

`fly.toml` scales to zero when idle, which is what keeps it at a couple of
dollars a month — and it means the first request of the day wakes a stopped
machine with an empty cache and no open connection to any upstream.

Three things narrow that gap without paying for an always-on machine. The page
calls `/api/config` on load, which wakes the machine before the pilot has typed
anything, and then `/api/prewarm`, which pulls the products that don't depend on
the route (the national SIGMET/AIRMET/CWA/G-AIRMET feeds and the home base) and
opens the upstream connections while they are still choosing a destination.
Startup parses the airport dataset in a thread rather than leaving it to the
first request. And the fetching itself is cheaper: a cold route is 19 upstream
requests rather than 24, all of them sharing pooled HTTP/2 connections to the
three hosts instead of opening a TLS session each.

None of it changes what the pilot is shown. The prewarm writes the same values
under the same cache keys with the same TTLs a live request would, and it is
deliberately outside `fetch_health.collect()` — a warmup can never put a banner
on the page, and a real request will re-fetch and report an outage honestly.

While an assessment runs, the elapsed time ticks next to the button and stays
there when it lands ("data fetched in 11.8 s"), so a long pull reads as work
rather than as a frozen app.

## Project layout

```
app/
  main.py            FastAPI routes + static UI
  config.py          settings + limits loader
  models.py          pydantic models
  orchestrator.py    assembles live data into route assessment / discovery
  sources/           cfps, awc (aviationweather.gov), openmeteo (HRDPS),
                     geomet (radar), airports,
                     cache (TTL + single-flight coalescing),
                     _http (one GET, retried once, over a pooled HTTP/2
                     connection shared by every upstream)
  services/          geo, geometry (does the route cross this area?),
                     area_products (reading a bulletin), area_hazards (one shape
                     for every advisory, and which ones reach this flight),
                     firs (which region a flight is in, coarsely),
                     runway, winds_aloft, weather (+TAF segments),
                     timeline, evaluator, density (density altitude),
                     etd_options (would waiting help?),
                     fetch_health (what failed to download)
data/                limits.yaml + bundled airport/runway seed
scripts/             refresh_airport_data.py (+ ensure_airport_data bootstrap),
                     probe_area_products.py (what the advisory feeds return),
                     probe_openmeteo_levels.py (which levels/variables are served)
web/                 single-page dashboard (Route + Discovery tabs)
tests/               offline logic tests + auto-skipping live smoke tests
```

### Light and dark

The **sun/moon button in the top-right of the header**, beside the clock, cycles
**Auto -> Light -> Dark**. Auto - the default - follows your device's setting,
and stays reachable on purpose: a plain two-state flip would strip it away on
the first tap with no way back. The icon shows which mode you are in (half-lit
circle for Auto), and its tooltip says what the next tap does.

The choice is per-browser (`localStorage`, key `minima.theme.v1`) and is
deliberately not part of your profile: it changes how the app looks, never how a
flight is assessed, so "Reset to defaults" leaves it alone.

This is a different thing from the **Day flight / Night flight** control on the
Route tab. That one is civil twilight, and it decides which set of your personal
minimums the flight is gated against - see "Day or night, and whether night is a
threat" above.

For anyone editing `web/style.css`: every colour lives in the two token blocks at
the top of that file, and rules reference them via `var()`. A tint is
`color-mix(in srgb, var(--token) N%, transparent)`, and a translucent overlay is
mixed from `--ink` rather than from white so it inverts with the theme. Adding a
light theme took one 20-line block because of this; `tests/test_theme.py` fails
if a colour literal creeps back into a rule.

## Configuration

Override via `FM_`-prefixed env vars (see `app/config.py`): `FM_ORIGIN`
(default departure), `FM_CRUISE_KT`, `FM_TIMELINE_HOURS`, `FM_OPENMETEO_MODEL`,
cache TTLs, upstream URLs. Decision-card thresholds live in `data/limits.yaml`.
