"""Static arena reference data (approximate city coordinates, standard UTC
offset and elevation). Used only for travel, time-zone and altitude features.
Neutral-site games are attributed to the listed home team's arena."""
import math

ARENAS = {  # abbreviation: (lat, lon, utc_offset_standard, elevation_ft)
    "ATL": (33.757, -84.396, -5, 1050), "BOS": (42.366, -71.062, -5, 20), "BKN": (40.683, -73.976, -5, 30),
    "CHA": (35.225, -80.839, -5, 750), "CHI": (41.881, -87.674, -6, 600), "CLE": (41.497, -81.688, -5, 650),
    "DAL": (32.790, -96.810, -6, 430), "DEN": (39.749, -105.008, -7, 5280), "DET": (42.341, -83.055, -5, 600),
    "GS": (37.768, -122.388, -8, 10), "HOU": (29.751, -95.362, -6, 50), "IND": (39.764, -86.156, -5, 715),
    "LAC": (33.945, -118.343, -8, 100), "LAL": (34.043, -118.267, -8, 270), "MEM": (35.138, -90.051, -6, 260),
    "MIA": (25.781, -80.188, -5, 10), "MIL": (43.045, -87.917, -6, 620), "MIN": (44.979, -93.276, -6, 830),
    "NO": (29.949, -90.082, -6, 5), "NY": (40.751, -73.993, -5, 30), "OKC": (35.463, -97.515, -6, 1200),
    "ORL": (28.539, -81.384, -5, 100), "PHI": (39.901, -75.172, -5, 40), "PHX": (33.446, -112.071, -7, 1090),
    "POR": (45.532, -122.667, -8, 50), "SAC": (38.580, -121.500, -8, 30), "SA": (29.427, -98.438, -6, 650),
    "TOR": (43.643, -79.379, -5, 250), "UTAH": (40.768, -111.901, -7, 4226), "UTA": (40.768, -111.901, -7, 4226),
    "WSH": (38.898, -77.021, -5, 50),
}


def miles(a, b):
    if a not in ARENAS or b not in ARENAS:
        return 0.0
    (la1, lo1, *_), (la2, lo2, *_) = ARENAS[a], ARENAS[b]
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2 - lo1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(min(1, math.sqrt(h)))


def tz(abbr):
    return ARENAS.get(abbr, (0, 0, -5, 0))[2]


def elevation(abbr):
    return ARENAS.get(abbr, (0, 0, 0, 0))[3]
