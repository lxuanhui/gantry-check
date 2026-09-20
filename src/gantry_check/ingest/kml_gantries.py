"""Parse OneMotoring's ERP gantry KML into `Gantry` objects.

The feed uses the *legacy* Google KML namespace (`http://earth.google.com/kml/2.2`), not the
OGC one, and each `<name>` is a CDATA blob of HTML rather than plain text.
"""

from __future__ import annotations

import csv
import html
import re
from pathlib import Path
from xml.etree import ElementTree

from gantry_check.domain.models import Gantry

KML_NS = "http://earth.google.com/kml/2.2"
_NS = {"kml": KML_NS}

SOURCE = "onemotoring-kml"

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
#: "CTE before Braddell Road (35)" -> ("CTE before Braddell Road", "35")
_NAME_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<number>\d+)\)$")


def _strip_html(markup: str) -> str:
    """Flatten the CDATA HTML of a <name> into its visible text."""
    text = _TAG_RE.sub(" ", markup)
    return _WHITESPACE_RE.sub(" ", html.unescape(text)).strip()


def parse_kml(data: bytes, zones: dict[str, str] | None = None) -> list[Gantry]:
    """Parse the ERP KML feed. `zones` maps gantry number -> Annex D zone id."""
    root = ElementTree.fromstring(data)
    placemarks = root.findall(".//kml:Placemark", _NS)
    if not placemarks:
        raise ValueError("no <Placemark> elements found; is this the legacy-namespace ERP KML?")

    gantries: list[Gantry] = []
    seen: set[str] = set()
    for index, placemark in enumerate(placemarks):
        name_el = placemark.find("kml:name", _NS)
        if name_el is None or not (name_el.text or "").strip():
            raise ValueError(f"Placemark #{index} has no <name>")
        label = _strip_html(name_el.text or "")
        match = _NAME_RE.match(label)
        if match is None:
            raise ValueError(f"Placemark #{index} name {label!r} has no trailing '(number)'")
        number = match.group("number")
        name = match.group("name")
        if not name:
            raise ValueError(f"Placemark #{index} name {label!r} has an empty location")
        if number in seen:
            raise ValueError(f"duplicate gantry number {number!r} in KML")
        seen.add(number)

        coords_el = placemark.find(".//kml:Point/kml:coordinates", _NS)
        coords = (coords_el.text or "").strip() if coords_el is not None else ""
        if not coords:
            raise ValueError(f"gantry {number} has no <Point><coordinates>")
        parts = coords.split(",")
        if len(parts) < 2:
            raise ValueError(f"gantry {number} has malformed coordinates {coords!r}")
        # KML orders coordinates lng,lat,altitude - the opposite of every lat/lng API we use.
        lng, lat = float(parts[0]), float(parts[1])

        gantries.append(
            Gantry(
                number=number,
                name=name,
                lat=lat,
                lng=lng,
                zone_id=(zones or {}).get(number),
                source=SOURCE,
            )
        )

    gantries.sort(key=lambda g: int(g.number))
    return gantries


def load_zones(csv_path: str | Path) -> dict[str, str]:
    """Read `data/static/annex_d_zones.csv` into {gantry_number: zone_id}."""
    zones: dict[str, str] = {}
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = {"zone_id", "gantry_number"} - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{csv_path}: missing column(s) {sorted(missing)}")
        for row in reader:
            number = (row["gantry_number"] or "").strip()
            zone_id = (row["zone_id"] or "").strip()
            if not number:
                continue
            if number in zones:
                raise ValueError(f"{csv_path}: duplicate gantry number {number!r}")
            zones[number] = zone_id
    return zones
