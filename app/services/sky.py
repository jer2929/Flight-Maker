"""What the sky is doing, in one sentence, from whichever source saw it.

The app has always *derived* a ceiling without a METAR - ``openmeteo.lowest_layer``
walks sixteen pressure levels of HRDPS cloud cover looking for the lowest broken
layer, and has done since the derivation was widened to stop swallowing whole
stratocumulus decks. What it did not do was *say* so. A sky with no broken layer
produced ``ceiling_agl_ft = None``, and every surface that printed a ceiling
printed nothing at all - so a clear sky, a scattered-only sky and a forecast that
never downloaded were one blank space, and a pilot reading the card could not
tell "nothing to worry about" from "you are flying blind".

This module turns the derivation's own four-way answer into the string the pages
render. One implementation, called once server-side, because the route checklist
row and the card chip describing the same sky in two different sentences is the
bug one layer up from the one this fixes.

Cloud **type** is observed or absent. ``CB``/``TCU`` come off the body group and
the genus (CU, SC, NS, AC ...) off the Canadian remarks; no forecast model
carries a cloud type, and inferring one from base height and thickness would
print a guess in the same notation as an observation.
"""
from __future__ import annotations

from app.models import Sky, SkyLayer, Source
from app.services import weather as wx
from app.sources import openmeteo

# Amounts that make a ceiling. ``VV`` is an indefinite ceiling - vertical
# visibility into an obscuration - and counts, which is the same rule
# ``weather._ceiling_from_sky`` applies to a METAR.
CEILING_AMOUNTS = ("BKN", "OVC", "VV")


def from_stack(stack: dict, source: Source = Source.MODEL) -> Sky:
    """A :class:`Sky` from ``openmeteo.cloud_stack``.

    Every layer is marked ``estimated``: it was interpolated from pressure-level
    cloud cover, not looked at, and the "~" the UI puts in front of its height is
    the whole of the difference.
    """
    if not stack or not stack.get("sampled"):
        return _rendered(Sky(state="unsampled", source=source))
    layers = [SkyLayer(amount=lyr["amount"], base_ft=lyr["base_ft"],
                       top_ft=lyr["top_ft"], estimated=True)
              for lyr in stack.get("layers", [])]
    return _rendered(Sky(layers=layers, state=_state(layers),
                         scan_top_ft=stack.get("scan_top_ft"), source=source))


def from_layers(layers: list[dict], source: Source, *,
                estimated: bool = False) -> Sky:
    """A :class:`Sky` from parsed report layers (``weather.observed_sky`` shape)."""
    built = [SkyLayer(amount=lyr["amount"], base_ft=lyr["base_ft"],
                      # ``VV`` is vertical visibility into an obscuration, not a
                      # layer with a genus. A type positionally matched onto one
                      # would be naming cloud nobody could see.
                      type=None if lyr["amount"] == "VV" else lyr.get("type"),
                      estimated=estimated)
             for lyr in layers]
    return _rendered(Sky(layers=built, state=_state(built), source=source))


def worse_sky(a: Sky | None, b: Sky | None) -> Sky | None:
    """Whichever of two skies a flight is more constrained by. See :func:`worst`."""
    return worst([a, b])


def from_metar(text: str | None, source: Source = Source.OBSERVED) -> Sky:
    """A :class:`Sky` from a raw METAR.

    A report with no cloud group is a genuine observation of a clear sky, and is
    the one place ``clear`` is stated without a "below" caveat: an observer looked
    up, where the model derivation has only ever been able to say "nothing below
    the top of the scan".
    """
    if not text:
        return _rendered(Sky(state="unsampled", source=source))
    return from_layers(wx.observed_sky(text), source)


def from_hourly(hourly: dict, i: int, elevation_ft: float | None,
                source: Source = Source.MODEL) -> Sky:
    """Convenience: the model's sky at one hour, straight off the raw series."""
    return from_stack(openmeteo.cloud_stack(hourly, i, elevation_ft), source)


def with_ceiling(sky: Sky | None, ceiling: float | None,
                 source: Source = Source.MODEL) -> Sky | None:
    """Guarantee a stack carries the ceiling the verdict was actually gated on.

    Two derivations can answer for one sky. Open-Meteo's ``cloud_base`` is a
    surface field; ``cloud_stack`` walks pressure levels. GEM - the model this app
    runs on - serves only the second, but a model serving the first would produce
    a card headlining a ceiling with a stack beside it that never mentioned one,
    which is the disagreement this whole change exists to remove.

    The ceiling wins, because it is the number every limit was checked against.
    Thinner layers below it survive: a scattered deck under the ceiling is still
    true, and still worth seeing.
    """
    if ceiling is None or (sky is not None and ceiling_ft(sky) == ceiling):
        return sky
    keep = [lyr for lyr in (sky.layers if sky else [])
            if lyr.amount not in CEILING_AMOUNTS and lyr.base_ft < ceiling]
    layers = keep + [SkyLayer(amount="BKN", base_ft=ceiling, estimated=True)]
    return _rendered(Sky(layers=layers, state="layers",
                         scan_top_ft=sky.scan_top_ft if sky else None,
                         source=source))


def ceiling_ft(sky: Sky | None) -> float | None:
    """The lowest broken-or-worse base in a stack, or None."""
    if sky is None:
        return None
    bases = [lyr.base_ft for lyr in sky.layers if lyr.amount in CEILING_AMOUNTS]
    return min(bases) if bases else None


def worst(skies: list[Sky | None]) -> Sky | None:
    """The sky a route is flown under: the one with the lowest ceiling.

    Falls back through the same ordering the states describe - a real ceiling
    beats a scattered layer beats a clear reading beats nothing sampled - so a
    route with one clear point and one unsampled one reports the clear one and
    does not quietly claim the whole route was seen.
    """
    known = [s for s in skies if s is not None]
    if not known:
        return None
    rank = {"layers": 0, "no_ceiling": 1, "clear": 2, "unsampled": 3}
    # Lowest ceiling first; then, among skies with the same ceiling (or none at
    # all), the one whose lowest layer sits lowest. ``min`` is stable, so an exact
    # tie keeps the earlier point - the departure end, which is the one a pilot
    # meets first.
    return min(known, key=lambda s: (rank.get(s.state, 3),
                                     ceiling_ft(s) if ceiling_ft(s) is not None else 1e9,
                                     _lowest_base(s)))


def describe(sky: Sky | None) -> str:
    """The one-liner every surface prints. See :class:`Sky` for the four states."""
    if sky is None or sky.state == "unsampled":
        return "not assessed - forecast did not download"
    if sky.state == "clear":
        # The model has never been able to see above the top of its scan, so it
        # says how far it looked. An observer has no such limit.
        if sky.source == Source.OBSERVED or sky.scan_top_ft is None:
            return "clear"
        # To the nearest 1,000 ft, unlike a layer base. This is not a measurement
        # of anything - it is where the pressure levels ran out - and printing it
        # to 100 ft would dress the scan's own limit up as a reading.
        return f"clear below ~{round(sky.scan_top_ft / 1000) * 1000:,.0f} ft AGL"
    text = " · ".join(_layer(lyr) for lyr in sky.layers)
    if sky.state == "no_ceiling":
        text += " · no broken layer"
    return text


def _layer(lyr: SkyLayer) -> str:
    if lyr.amount == "VV":
        return f"indefinite ceiling {_ft(lyr.base_ft)} ft"
    tilde = "~" if lyr.estimated else ""
    return (f"{lyr.amount} {tilde}{_ft(lyr.base_ft)} ft"
            + (f" {lyr.type}" if lyr.type else ""))


def _state(layers: list[SkyLayer]) -> str:
    if not layers:
        return "clear"
    return "layers" if any(lyr.amount in CEILING_AMOUNTS for lyr in layers) else "no_ceiling"


def _lowest_base(sky: Sky) -> float:
    return min((lyr.base_ft for lyr in sky.layers), default=0.0)


def _ft(ft: float | None) -> str:
    """Heights to the nearest 100 ft, the resolution the derivation actually has."""
    return "-" if ft is None else f"{round(ft / 100) * 100:,.0f}"


def _rendered(sky: Sky) -> Sky:
    sky.text = describe(sky)
    return sky
