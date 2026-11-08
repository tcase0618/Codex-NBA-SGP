"""Regenerate the 440 metric slots from the supplied blueprint's extracted text."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
lines = [x.strip() for x in (ROOT / "docs/blueprint-extracted.txt").read_text(encoding="utf-8").splitlines()]
lines = [x for x in lines if x and not x.startswith("NBA QUANT |") and not re.match(r"^Page \d+$", x)]

def groups(start, end, scope, size):
    section = lines[lines.index(start) + 1:lines.index(end)]
    result = []
    for i, line in enumerate(section):
        if re.match(r"^\d+\. ", line):
            category = re.sub(r"^\d+\. ", "", line)
            for label in section[i + 1:i + 1 + size]:
                result.append({"scope": scope, "category": category, "label": label})
    return result

metrics = groups("A2. Universal player analytics — 100 metric slots", "A3. Position-specific player modules — 20 each", "player", 10)
positions = {
    "PG": ("Point guard — 20 specialized metrics", "Offensive creation and decision-making", "Control, pressure and adaptability"),
    "SG": ("Shooting guard — 20 specialized metrics", "Shot creation and scoring", "Off-ball movement and defense"),
    "SF": ("Small forward — 20 specialized metrics", "Wing offense and versatility", "Wing defense"),
    "PF": ("Power forward — 20 specialized metrics", "Frontcourt offense", "Frontcourt defense"),
    "C": ("Center — 20 specialized metrics", "Interior offense and rebounding", "Rim protection and defensive schemes"),
}
for position, (_, *categories) in positions.items():
    for category in categories:
        start = lines.index(category) + 1
        metrics.extend({"scope": position, "category": category, "label": label} for label in lines[start:start + 10])
metrics.extend(groups("A4. Team intelligence — 240 metric slots", "A5. Connecting the player and team layers", "team", 12))
assert len(metrics) == 440, len(metrics)
for i, metric in enumerate(metrics):
    label = metric["label"].lower()
    metric["id"] = f"{metric['scope'].lower()}-{i + 1:03d}"
    tracking_words = ("gravity", "speed", "distance", "acceleration", "deceleration", "positioning", "coverage", "navigation", "containment", "rotation quality", "deterrence", "expected", "quality", "matchup", "scheme", "contested", "touch", "pass", "drive", "screen", "boxout", "contest", "blitz", "hedge", "switch", "drop", "zone")
    metric["required_source"] = "tracking_or_custom_model" if any(w in label for w in tracking_words) else "box_score_or_play_by_play"
    metric["status"] = "UNAVAILABLE"
    metric["value"] = None
    metric["source_reference"] = "NBA Quant blueprint, Part A"
    metric["unit"] = "percent" if "%" in label or "share" in label else "per_100_possessions" if "/ 100" in label else "metric_specific"
    metric["implementation"] = "definition_slot; formula and provider mapping required before activation"
(ROOT / "nba_quant/metric_catalog.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
print({scope: sum(m["scope"] == scope for m in metrics) for scope in ["player", *positions, "team"]})
