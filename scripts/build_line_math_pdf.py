"""Build the every-line accounting audit from immutable review JSON artifacts.

Run: python scripts/build_line_math_pdf.py
Render: pdftoppm -scale-to 1600 -png <output PDF> tmp/pdfs/line-math/page
This script does not fetch sources, change selections, or rerun a model.
"""
import argparse
import hashlib
import json
import math
from collections import Counter, OrderedDict
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "output/review-2026-11-04/line-math-review.json"
REPLAY = ROOT / "output/review-2026-11-04/selection-development-replay.json"
OUTPUT = ROOT / "output/pdf/CASECAP_NBA_QUANT_Every_Line_Math_Review_2026-11-04.pdf"
W, H = landscape(A4)
M = 42
NAVY = colors.HexColor("#122B3A")
TEAL = colors.HexColor("#177C73")
ORANGE = colors.HexColor("#B66B24")
RED = colors.HexColor("#A44240")
INK = colors.HexColor("#253B49")
MUTED = colors.HexColor("#667B88")
RULE = colors.HexColor("#D8E2E6")
PALE = colors.HexColor("#F4F7F8")
FONTS = ("Body", "Bold", "Mono")
STATS = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM"}


def fonts():
    system = Path("C:/Windows/Fonts")
    choices = [("Body", "segoeui.ttf"), ("Bold", "segoeuib.ttf"), ("Mono", "consola.ttf")]
    for name, filename in choices:
        path = system / filename
        if not path.exists():
            raise RuntimeError(f"Required font missing: {path}")
        pdfmetrics.registerFont(TTFont(name, str(path)))


def signed(value, digits=3):
    return f"{value:+.{digits}f}" if value is not None else "unavailable"


def number(value, digits=2):
    return f"{value:.{digits}f}" if value is not None else "DNP"


def line(c, text, x, y, size=9, font="Body", color=INK, max_width=None):
    c.setFillColor(color)
    c.setFont(font, size)
    if max_width is not None and pdfmetrics.stringWidth(text, font, size) > max_width + .01:
        raise ValueError(f"Text exceeds width ({max_width}): {text}")
    c.drawString(x, y, text)


def paragraph(c, text, x, top, width, size=10, leading=15, color=INK, bold=False):
    style = ParagraphStyle("text", fontName="Bold" if bold else "Body", fontSize=size,
                           leading=leading, textColor=color, spaceAfter=0)
    p = Paragraph(text, style)
    _, height = p.wrap(width, H)
    p.drawOn(c, x, top - height)
    return top - height


def header(c, page, section, total=16):
    c.setFillColor(NAVY)
    c.rect(0, H - 39, W, 39, fill=1, stroke=0)
    line(c, "CASECAP", M, H - 25, 13, "Bold", colors.white)
    line(c, "NBA QUANT", M + 88, H - 24, 10, "Body", colors.HexColor("#A9D7D2"))
    c.setFont("Body", 8)
    c.setFillColor(colors.HexColor("#C5D4DC"))
    c.drawRightString(W - M, H - 24, "NOVEMBER 04, 2026  /  EVERY-LINE MATH REVIEW")
    c.setStrokeColor(RULE)
    c.line(M, 29, W - M, 29)
    line(c, "RESEARCH AUDIT  |  DESCRIPTIVE ACCOUNTING, NOT CAUSAL ATTRIBUTION", M, 16, 6.8, color=MUTED)
    c.setFont("Mono", 7)
    c.drawRightString(W - M, 16, f"{section.upper()}  /  {page:02d} OF {total:02d}")


def title(c, eyebrow, heading, subtitle=None):
    line(c, eyebrow.upper(), M, H - 66, 8.5, "Bold", TEAL)
    line(c, heading, M, H - 97, 24, "Bold", NAVY, W - 2 * M)
    if subtitle:
        paragraph(c, subtitle, M, H - 111, W - 2 * M, 9.1, 13, MUTED)


def metric(c, x, y, width, label, value, note, accent=TEAL):
    c.setFillColor(PALE)
    c.roundRect(x, y - 86, width, 86, 5, fill=1, stroke=0)
    line(c, label.upper(), x + 14, y - 18, 7.8, "Bold", MUTED)
    line(c, value, x + 14, y - 49, 24, "Bold", accent)
    line(c, note, x + 14, y - 69, 7.7, color=MUTED, max_width=width - 28)


def section(c, heading, x, top, width, body, size=9.2):
    line(c, heading, x, top, 12, "Bold", NAVY)
    return paragraph(c, body, x, top - 13, width, size, 13.2, INK)


def summary_page(c, doc, source_hash):
    s = doc["summary"]
    header(c, 1, "Results")
    title(c, "One slate. Every distinct selection.", "The historical screen overstated confidence.",
          "Frozen draft selections compared with the supplied postgame source records. Repeated appearances in 3-, 5-, and 8-leg families are deduplicated.")
    gap, width = 14, (W - 2 * M - 42) / 4
    resolved = s["status_counts"]["PASS"] + s["status_counts"]["FAIL"]
    metric(c, M, 437, width, "Resolved selection hit rate", f"{86 / resolved:.2%}", "86 PASS / 108 resolved")
    metric(c, M + width + gap, 437, width, "Selected historical mean", f"{s['mean_historical_frequency_resolved']:.2%}", "In-sample; NOT a forecast", ORANGE)
    metric(c, M + 2 * (width + gap), 437, width, "Pregame probabilities", "0 / 109", "No calibrated chance stored", ORANGE)
    metric(c, M + 3 * (width + gap), 437, width, "Offered prices", "0 / 109", "Value and ROI not measurable", ORANGE)
    left, right, col = M, W / 2 + 14, (W - 2 * M - 28) / 2
    section(c, "What the 109 distinct lines establish", left, 326, col,
            "<b>86 PASS, 22 FAIL, 1 DNP.</b> These 109 selections appeared 216 times in nested families across 14 games. "
            "They are not 216 independent bets, and the DNP is excluded from the 108 resolved-stat denominator.<br/><br/>"
            "All <b>109 availability states were UNVERIFIED</b>. Prior samples contained only <b>3-8 positive-minute appearances</b>. "
            "The historical mean of 95.98% is the average selected-threshold hit frequency for resolved lines, not a model-implied next-game chance.")
    line(c, "By-stat accounting", right, 326, 12, "Bold", NAVY)
    xcols = [right, right + 81, right + 158, right + 263]
    for x, text in zip(xcols, ["STAT", "PASS / N", "HISTORICAL", "ACTUAL"]):
        line(c, text, x, 302, 7.5, "Bold", MUTED)
    for i, (key, group) in enumerate(s["groups"].items()):
        y = 277 - i * 25
        c.setStrokeColor(RULE)
        c.line(right, y - 10, right + col, y - 10)
        values = [STATS[key], f"{group['passed']} / {group['resolved']}",
                  f"{group['historical_frequency_mean']:.2%}", f"{group['actual_hit_rate']:.2%}"]
        for x, text in zip(xcols, values):
            line(c, text, x, y, 9, "Mono", TEAL if text == values[-1] else INK)
    c.setFillColor(colors.HexColor("#FFF3E6"))
    c.roundRect(M, 91, W - 2 * M, 72, 5, fill=1, stroke=0)
    line(c, "22 misses: 19 had a larger negative rate component; 3 had a larger minutes component.", M + 16, 141, 10, "Bold", ORANGE)
    paragraph(c, "This separates realized stat production into an exact arithmetic identity. It does <b>not</b> prove why a player "
              "played fewer minutes, attempted fewer shots, shot worse, or accumulated fewer assists or rebounds. Coaching, defense, injury and shot-quality causes remain unverified.",
              M + 16, 128, W - 2 * M - 32, 8.8, 12, INK)
    line(c, f"Source as of: {doc['source_as_of']}  |  Source SHA-256: {source_hash}", M, 53, 6.5, "Mono", MUTED, W - 2 * M)
    c.showPage()


def method_page(c, doc, replay):
    header(c, 2, "Methods + corrections")
    title(c, "The math behind each line", "Accounting first. Causal claims stay unverified.")
    left, right, col = M, W / 2 + 14, (W - 2 * M - 28) / 2
    y = section(c, "Exact minutes / realized-rate identity", left, 455, col,
        "Let H be prior mean production, M0 prior mean minutes, A actual production and M1 actual minutes. "
        "The reference rate r0 = H / M0; actual rate r1 = A / M1.<br/><br/>"
        "<b>A - H = (M1 - M0) x r0 + M1 x (r1 - r0)</b><br/>"
        "The first term is the minutes component; the second is the realized-rate component. Components use unrounded inputs, then print to three decimals. "
        "A displayed sum can differ by 0.001 because of rounding.<br/><br/>"
        "For made threes, repeat the identity using 3PA as exposure and 3PM/3PA as conversion. These are alternate decompositions of the same 3PM delta; never add the two decompositions together.")
    y = section(c, "Reading the game pages", left, y - 26, col,
        "Every card identifies the threshold, PASS/FAIL/DNP, actual result, family membership, original historical h/n screen, "
        "prior mean and range, prior/actual minutes, and the exact delta components. The 3PM cards add attempts-versus-conversion math when available.<br/><br/>"
        "<b>Original selection rule:</b> highest stat ladder meeting at least 80% historical frequency, with both teams represented, distinct players and at most two selections per stat. "
        "A PASS is simply a threshold met in the supplied outcome; it is not a settled sportsbook payout.", size=8.8)
    section(c, "Corrections now implemented: construction 0.3.0", right, 455, col,
        "<b>1. Uncertainty-aware ranking.</b> Candidates use a Wilson lower-support ranking rather than raw hit frequency alone. "
        "Small samples and thresholds selected on that same sample still invalidate any interpretation as calibrated confidence.<br/><br/>"
        "<b>2. Complete feasibility search.</b> The prior greedy selector falsely blocked Milwaukee's 8-leg family. Full feasibility search resolves that construction error; the newly feasible family loses in the development replay.<br/><br/>"
        "<b>3. Full sample evidence.</b> Retain all historical appearances used, threshold-selection evidence and source provenance. New code does not rewrite the original frozen artifacts.")
    original = replay["original_counts"]
    revised = replay["revised_development_counts"]
    c.setFillColor(PALE)
    c.roundRect(right, 131, col, 133, 5, fill=1, stroke=0)
    line(c, "DEVELOPMENT REPLAY, NOT A HOLDOUT", right + 14, 244, 8, "Bold", ORANGE)
    paragraph(c, f"Fixed original candidates; <b>{replay['changed_families']} family selections changed.</b><br/>"
              f"Original: {original['HIT']} HIT / {original['MISS']} MISS / {original['DNP_RULES_UNVERIFIED']} DNP / {original['BLOCKED']} blocked.<br/>"
              f"Revised: {revised['HIT']} HIT / {revised['MISS']} MISS / {revised['DNP_RULES_UNVERIFIED']} DNP.<br/><br/>"
              "<b>No demonstrated accuracy gain.</b> This already-reviewed slate is development evidence, not an independent performance test. No parameters were fitted to these outcomes.",
              right + 14, 229, col - 28, 8.7, 12.3)
    paragraph(c, "<b>Separate future-model correction:</b> the simulator now preserves the intended make probability when resolving blocks. "
              "With q = 0.53 and block probability b = 0.055, the old sequence yielded (1 - b) x q = 0.50085; the corrected sequence yields 0.53. "
              "<b>This simulator was not used to construct the original tickets.</b>",
              M, 119, W - 2 * M, 8.3, 11.5, INK)
    paragraph(c, "<b>Source artifacts:</b> line-math-review.json and selection-development-replay.json in output/review-2026-11-04/. "
              "Full prior samples, game IDs, box counts and bundle IDs remain in the JSON. Per-game source URLs and frozen bundle hashes appear on the game pages. "
              "No offered prices, prospective calibration, book settlement, ROI, or causal diagnosis is available from this audit.",
              M, 68, W - 2 * M, 7.8, 10.8, MUTED)
    c.showPage()


def card(c, entry, x, top, width, height=96):
    bottom = top - height
    outcome = entry["result"]
    status_color = TEAL if outcome == "PASS" else RED if outcome == "FAIL" else ORANGE
    c.setStrokeColor(RULE)
    c.setFillColor(colors.white)
    c.roundRect(x, bottom, width, height, 4, fill=1, stroke=1)
    c.setFillColor(status_color)
    c.roundRect(x, bottom, 3, height, 1, fill=1, stroke=0)
    line(c, entry["player"], x + 13, top - 17, 10.5, "Bold", NAVY, width - 85)
    c.setFont("Bold", 8.5)
    c.setFillColor(status_color)
    c.drawRightString(x + width - 12, top - 16, outcome)
    families = "/".join(str(n) for n in entry["families"])
    threshold = f"{STATS[entry['stat']]} {entry['line']:g}+"
    actual = "DNP" if entry["actual"] is None else f"{entry['actual']:g}"
    texts = [
        (f"{threshold}   |   ACTUAL {actual}   |   FAMILIES {families}", "Bold", INK),
        (f"WHY CHOSEN {entry['historical_hits']}/{entry['historical_games']} ({entry['historical_hit_rate']:.1%})   |   PRIOR MEAN {entry['historical_mean']:.3f}", "Body", INK),
        (f"PRIOR RANGE {entry['historical_min']:g}-{entry['historical_max']:g}   |   MIN prior {number(entry['historical_minutes'])} / actual {number(entry['actual_minutes'])}", "Body", MUTED),
    ]
    decomposition = entry["minutes_rate_decomposition"]
    if decomposition:
        texts.append((f"DELTA {signed(decomposition['total_difference'])} = MIN {signed(decomposition['exposure_component'])} + RATE {signed(decomposition['rate_component'])}", "Mono", INK))
        three = entry["three_attempt_conversion_decomposition"]
        if three:
            texts.append((f"3PM: 3PA {signed(three['exposure_component'])} + CONV {signed(three['rate_component'])} = {signed(three['total_difference'])}", "Mono", TEAL))
        else:
            rate = decomposition["reference_rate"]
            texts.append((f"RATE / MIN prior {rate:.4f} / actual {decomposition['actual_rate']:.4f}", "Mono", MUTED))
        difference = entry["actual"] - entry["line"]
        interpretation = f"Threshold met by {difference:g}." if outcome == "PASS" else f"Threshold missed by {-difference:g}."
        texts.append((f"{interpretation} Accounting only; cause unverified.", "Body", MUTED))
    else:
        texts.append(("DNP: no positive-minute outcome; stat delta not computed.", "Body", ORANGE))
        texts.append(("Participation unmodeled; book void rules unverified.", "Body", MUTED))
        texts.append(("Excluded from resolved-stat hit-rate denominator.", "Body", MUTED))
    for i, (text, font, color) in enumerate(texts):
        line(c, text, x + 13, top - 32 - i * 11, 7.8 if font != "Mono" else 7.3,
             font, color, width - 26)
    return bottom


def game_page(c, page, game_id, rows, doc):
    header(c, page, "Game detail")
    game_name = rows[0]["game"]
    counts = Counter(e["result"] for e in rows)
    line(c, f"GAME {game_id}  /  NOVEMBER 04 SLATE", M, H - 61, 8, "Bold", TEAL)
    title_size = 22 if len(game_name) < 48 else 20
    line(c, game_name, M, H - 88, title_size, "Bold", NAVY, W - 2 * M)
    line(c, f"{len(rows)} distinct selections  |  {counts['PASS']} PASS / {counts['FAIL']} FAIL / {counts['DNP']} DNP  |  Availability: all UNVERIFIED  |  No stored forecast probabilities or prices",
         M, H - 107, 8, color=MUTED, max_width=W - 2 * M)
    col_gap, row_gap = 18, 8
    width = (W - 2 * M - col_gap) / 2
    top = H - 124
    for i, entry in enumerate(rows):
        card(c, entry, M + (i % 2) * (width + col_gap), top - (i // 2) * (96 + row_gap), width)
    if len(rows) < 8:
        y = top - math.ceil(len(rows) / 2) * (96 + row_gap)
        paragraph(c, "<b>Original 8-leg family was blocked.</b> Only the original five distinct selections appear here. "
                  "The complete-search correction makes an 8-leg family feasible in the development replay, where it loses. "
                  "Revised selections are not retroactively inserted into this original-line review.", M, y - 5, W - 2 * M, 9, 13, MUTED)
    source = rows[0]["source_url"]
    line(c, "OUTCOME SOURCE  " + source, M, 50, 6.7, color=MUTED, max_width=W - 2 * M)
    c.linkURL(source, (M, 47, W - M, 58), relative=0, thickness=0)
    line(c, "FROZEN BUNDLE SHA-256  " + doc["bundle_hashes"][game_id], M, 39, 6.5, "Mono", MUTED, W - 2 * M)
    c.showPage()


def validate(doc, replay):
    entries = doc["entries"]
    assert len(entries) == doc["summary"]["unique_selections"] == 109
    assert len({(e["game_id"], e["player_id"], e["stat"], e["side"], e["line"]) for e in entries}) == 109
    assert Counter(e["result"] for e in entries) == Counter({"PASS": 86, "FAIL": 22, "DNP": 1})
    assert sum(len(e["families"]) for e in entries) == 216
    assert all(e["availability"] == "UNVERIFIED" for e in entries)
    assert all(e["offered_price"] is None and e["pregame_forecast_probability"] is None for e in entries)
    assert replay["version"] == "analytical-draft-0.3.0"
    for e in entries:
        assert len(e["sample"]) == e["historical_games"]
        for key in ("minutes_rate_decomposition", "three_attempt_conversion_decomposition"):
            d = e[key]
            if d:
                assert math.isclose(d["exposure_component"] + d["rate_component"], d["total_difference"], abs_tol=1e-9)
                assert math.isclose(d["total_difference"], e["actual"] - e["historical_mean"], abs_tol=1e-9)


def build(source=SOURCE, replay_path=REPLAY, output=OUTPUT):
    doc = json.loads(source.read_text(encoding="utf-8"))
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    validate(doc, replay)
    groups = OrderedDict()
    for entry in doc["entries"]:
        groups.setdefault(entry["game_id"], []).append(entry)
    assert len(groups) == 14 and max(map(len, groups.values())) == 8
    fonts()
    output.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output), pagesize=(W, H), invariant=True, pageCompression=1)
    c.setTitle("CASECAP NBA QUANT - Every-Line Math Review - November 04, 2026")
    c.setAuthor("Case Capital Research")
    c.setSubject("Frozen draft selection outcomes, sample support, and exact minutes/rate accounting")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    summary_page(c, doc, source_hash)
    method_page(c, doc, replay)
    for page, (game_id, rows) in enumerate(groups.items(), 3):
        game_page(c, page, game_id, rows, doc)
    c.save()
    from pypdf import PdfReader
    reader = PdfReader(output)
    assert len(reader.pages) == 16
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert all(e["player"] in text for e in doc["entries"])
    assert text.count("WHY CHOSEN") == 109
    receipt = {"output": str(output), "pages": len(reader.pages), "distinct_lines": 109,
        "source_sha256": source_hash, "replay_sha256": hashlib.sha256(replay_path.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "verified": ["source counts", "all 109 players represented", "109 WHY CHOSEN cards", "exact component identities", "16 pages"],
        "visual_inspection": "pending"}
    print(json.dumps(receipt, indent=2))
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--replay", type=Path, default=REPLAY)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    build(args.source, args.replay, args.output)
