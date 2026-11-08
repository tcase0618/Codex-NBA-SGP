"""Create the CASECAP NBA QUANT draft book from the exact exported tickets."""
import json
import argparse
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib.colors import HexColor, Color, white
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/pdf/CASECAP_NBA_QUANT_Parlay_Drafts_2026-11-04.pdf"
ROWS = json.loads((ROOT / "output/tonight-parlays-export.json").read_text(encoding="utf-8"))
META = {}
pdfmetrics.registerFont(TTFont("CaseRegular", "C:/Windows/Fonts/arial.ttf"))
pdfmetrics.registerFont(TTFont("CaseBold", "C:/Windows/Fonts/arialbd.ttf"))
pdfmetrics.registerFontFamily("CaseRegular", normal="CaseRegular", bold="CaseBold")
NAVY = HexColor("#101E2A")
TEAL = HexColor("#087D77")
GOLD = HexColor("#D6B36B")
GRAY = HexColor("#536372")
LINE = HexColor("#DCE3E8")
PALE = HexColor("#F1F5F6")
W, H, M = 612, 792, 42
STATS = {"pts": "POINTS", "reb": "REBOUNDS", "ast": "ASSISTS", "fg3m": "MADE THREES"}


def paragraph(c, text, x, top, width, size=9, leading=12, color=NAVY, bold=False):
    style = ParagraphStyle("body", fontName="CaseBold" if bold else "CaseRegular", fontSize=size,
                           leading=leading, textColor=color, alignment=TA_LEFT)
    p = Paragraph(text, style)
    _, height = p.wrap(width, 1000)
    p.drawOn(c, x, top - height)
    return height


def footer(c, number, total):
    c.setStrokeColor(LINE)
    c.line(M, 39, W - M, 39)
    c.setFillColor(GRAY)
    c.setFont("CaseRegular", 7)
    c.drawString(M, 25, "CASECAP NBA QUANT  |  NOVEMBER 4, 2026  |  RESEARCH DRAFTS")
    c.drawRightString(W - M, 25, f"{number:02d} / {total:02d}")


def game_block(c, game, top, sequence):
    c.setFillColor(TEAL)
    c.setFont("CaseBold", 8)
    tip = datetime.fromisoformat(game["tipoff"]).astimezone(ZoneInfo("America/New_York"))
    time = tip.strftime("%I:%M %p").lstrip("0")
    c.drawString(M, top, f"MATCHUP {sequence:02d}  /  {time} ET")
    paragraph(c, escape(game["game"]), M, top - 11, W - 2 * M, 14, 17, NAVY, True)
    c.setFillColor(GRAY)
    c.setFont("CaseRegular", 7.5)
    c.drawString(M, top - 43, "Full tickets shown independently. Every threshold means at least.")
    y = top - 56
    gap = 12
    col_width = (W - 2 * M - 2 * gap) / 3
    for index, family in enumerate(game["families"]):
        x = M + index * (col_width + gap)
        c.setFillColor(NAVY)
        c.roundRect(x, y - 23, col_width, 23, 3, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("CaseBold", 9)
        c.drawString(x + 10, y - 15, f"{family['size']}-LEG")
        c.setFont("CaseRegular", 6.5)
        c.drawRightString(x + col_width - 9, y - 15, "BLOCKED" if not family["legs"] else "UNPRICED")
        if not family["legs"]:
            c.setFillColor(PALE)
            c.roundRect(x, y - 142, col_width, 111, 3, fill=1, stroke=0)
            paragraph(c, "Insufficient qualifying players and mixed-stat coverage.", x + 11, y - 44,
                      col_width - 22, 10, 14, GRAY)
            paragraph(c, "No ticket was constructed.", x + 11, y - 108, col_width - 22, 8, 11, GRAY, True)
            continue
        cursor = y - 34
        for leg_number, leg in enumerate(family["legs"], 1):
            c.setFillColor(GRAY)
            c.setFont("CaseRegular", 6)
            c.drawString(x + 1, cursor - 7, str(leg_number))
            height = paragraph(c, escape(leg["name"]), x + 12, cursor, col_width - 15, 8.4, 10, NAVY, True)
            c.setFillColor(TEAL)
            c.setFont("CaseBold", 7)
            assert leg["side"] == "at_least"
            c.drawString(x + 12, cursor - height - 9, f"{int(leg['line'])}+ {STATS[leg['stat']]}")
            cursor -= max(27, height + 17)
        assert cursor >= top - 320, (game["id"], family["size"], cursor, top)


def receipt_page(c, total, count, blocked):
    c.setFillColor(NAVY)
    c.rect(0, H - 67, W, 67, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("CaseBold", 14)
    c.drawString(M, H - 29, "CASECAP NBA QUANT")
    c.setFillColor(GOLD)
    c.setFont("CaseRegular", 7.5)
    c.drawString(M, H - 47, "REFRESHED SCAN / SOURCE RECEIPT")
    scanned = datetime.fromisoformat(META["scanned_at"]).astimezone(ZoneInfo("America/New_York"))
    paragraph(c, "Scan completed " + scanned.strftime("%B %d, %Y at %I:%M %p ET"), M, 703, W - 2*M, 16, 20, NAVY, True)
    paragraph(c, f"{len(ROWS)} games | {count} constructed drafts | {blocked} blocked family | "
              f"{META['priced_draft_legs']} source-priced draft legs", M, 672, W - 2*M, 9, 13, TEAL, True)
    paragraph(c, "WHAT CHANGED", M, 635, W - 2*M, 9, 12, TEAL, True)
    paragraph(c, "The refreshed roster and construction scan changed these draft sets: " +
              escape("; ".join(META.get("changed_matchups", [])) or "No matchup leg sets changed") +
              ". The earlier PDF is retained as a separate snapshot.", M, 614, W - 2*M, 10, 15)
    paragraph(c, "ALT-LINE REVIEW", M, 555, W - 2*M, 9, 12, TEAL, True)
    paragraph(c, "Standard and alternate targets are supported across player statistics, including integer milestones and half-line over/unders. "
              "No verified current offered alternate prices were available for this scan. Draft thresholds therefore remain analytical; "
              "no unvalidated matchup boost was used to raise a target.", M, 535, W - 2*M, 10, 15)
    paragraph(c, "PUBLISHED FANDUEL SNAPSHOTS - SEPARATE FROM THESE TICKETS", M, 450, W - 2*M, 9, 12, TEAL, True)
    paragraph(c, "Automatically retrieved primary-source quotes. Quote update times are unavailable; these are not confirmed current prices or combined SGP payouts.", M, 430, W - 2*M, 9, 13, GRAY)
    y = 384
    c.setFillColor(NAVY)
    c.rect(M, y - 21, W - 2*M, 21, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("CaseBold", 8)
    c.drawString(M + 10, y - 14, "PLAYER")
    c.drawString(M + 208, y - 14, "PUBLISHED SELECTION")
    c.drawRightString(W - M - 10, y - 14, "ODDS")
    for i, offer in enumerate(META.get("published_offers", [])):
        row_y = y - 21 - i * 30
        c.setFillColor(PALE if i % 2 == 0 else white)
        c.rect(M, row_y - 30, W - 2*M, 30, fill=1, stroke=0)
        paragraph(c, escape(offer["player_name"]), M + 10, row_y - 9, 185, 9, 11, NAVY, True)
        stat = STATS.get(offer["stat"], "PTS + REB + AST" if offer["stat"] == "pra" else offer["stat"].upper())
        value = f"{offer['line']:g}+" if offer["side"] == "at_least" else f"{offer['side'].upper()} {offer['line']:g}"
        paragraph(c, escape(value + " " + stat), M + 208, row_y - 9, 223, 8.5, 11)
        c.setFillColor(TEAL)
        c.setFont("CaseBold", 10)
        c.drawRightString(W - M - 10, row_y - 18, f"{offer['american_odds']:+g}")
    paragraph(c, "FRESHNESS UNKNOWN | " + str(META.get("matched_published_offers", 0)) +
              " of " + str(META.get("published_offers_count", 0)) + " published quotes matched the current roster/game identities. "
              "The unmatched quote is retained here as source evidence, not assigned to a ticket.", M, 239, W - 2*M, 8.5, 12, GRAY)
    paragraph(c, "SOURCE / SCOPE", M, 190, W - 2*M, 9, 12, TEAL, True)
    paragraph(c, "Schedule and current roster context: ESPN NBA, observed during this scan. Historical source: 100 validated completed games; "
              "seven rejected source records remain excluded. Roster membership is not a confirmed active-game lineup.", M, 171, W - 2*M, 8.5, 12, GRAY)
    paragraph(c, "Published quotes: FanDuel Research, NBA Props Today, November 4, 2026.<br/>"
              '<link href="https://www.fanduel.com/research/nba-props-today-player-prop-bets-wednesday-nov-4" color="#087D77">'
              "fanduel.com/research/nba-props-today-player-prop-bets-wednesday-nov-4</link>", M, 119, W - 2*M, 8, 11, GRAY)
    footer(c, total, total)
    c.showPage()


def build():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    count = sum(bool(f["legs"]) for g in ROWS for f in g["families"])
    blocked = sum(not f["legs"] for g in ROWS for f in g["families"])
    assert len(ROWS) == 14 and count + blocked == 42
    total = 9 if META else 8
    c = canvas.Canvas(str(OUT), pagesize=(W, H))
    c.setTitle("CASECAP NBA QUANT | November 4, 2026 | SGP Draft Book")
    c.setAuthor("CASECAP NBA QUANT")
    c.setSubject(f"{count} unpriced analytical SGP drafts across 14 NBA matchups")
    c.setFillColor(NAVY)
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(GOLD)
    c.rect(M, H - 76, 38, 3, fill=1, stroke=0)
    c.setFont("CaseBold", 16)
    c.drawString(M, H - 111, "CASECAP")
    c.setFillColor(white)
    c.setFont("CaseBold", 28)
    c.drawString(M, H - 149, "NBA QUANT")
    c.setFillColor(HexColor("#A6BCCB"))
    c.setFont("CaseRegular", 10)
    c.drawString(M, H - 172, "PLAYER + TEAM INTELLIGENCE")
    paragraph(c, "TONIGHT'S<br/>SGP DRAFT BOOK", M, 555, W - 2 * M, 34, 40, white, True)
    c.setFillColor(GOLD)
    c.setFont("CaseBold", 12)
    c.drawString(M, 447, "NOVEMBER 4, 2026  /  ALL TIMES EASTERN")
    for i, (value, label) in enumerate([("14", "MATCHUPS"), (str(count), "DRAFT TICKETS"), (f"{blocked:02d}", "BLOCKED FAMILIES")]):
        x = M + i * 180
        c.setStrokeColor(HexColor("#39505F"))
        c.line(x, 414, x + 151, 414)
        c.setFillColor(white)
        c.setFont("CaseBold", 29)
        c.drawString(x, 376, value)
        c.setFillColor(HexColor("#A6BCCB"))
        c.setFont("CaseRegular", 8)
        c.drawString(x, 356, label)
    c.setFillColor(HexColor("#1D3343"))
    c.roundRect(M, 184, W - 2 * M, 131, 5, fill=1, stroke=0)
    paragraph(c, "UNPRICED RESEARCH DRAFTS", M + 17, 296, W - 2 * M - 34, 10, 13, GOLD, True)
    paragraph(c, "These are the saved analytical tickets, not verified FanDuel offers or approved wagers. "
              "Game-day eligibility, joint hit probabilities and combined payouts remain unverified. "
              "No odds, expected returns or accuracy claims are supplied.", M + 17, 273, W - 2 * M - 34, 10, 14, white)
    paragraph(c, "HOW TO READ THIS BOOK", M, 154, W - 2 * M, 8, 11, GOLD, True)
    paragraph(c, "Each 3-, 5- and 8-leg ticket lists every leg in full. Each player may appear once per ticket. "
              "A blocked family has no generated ticket. All thresholds mean 'at least'.", M, 135, W - 2 * M, 9, 13, HexColor("#C3D2DB"))
    c.setFillColor(HexColor("#A6BCCB"))
    c.setFont("CaseRegular", 7)
    stamp = datetime.fromisoformat(META["scanned_at"]).astimezone(ZoneInfo("America/New_York")) if META else None
    c.drawString(M, 48, "REFRESHED SCAN: " + stamp.strftime("NOV %d, %Y, %I:%M %p ET") if stamp else "TICKET SNAPSHOT: NOV 4, 2026, 5:46 PM ET | EXPORTED 5:52 PM ET")
    c.drawRightString(W - M, 48, f"01 / {total:02d}")
    c.showPage()
    for page in range(7):
        c.setFillColor(NAVY)
        c.rect(0, H - 67, W, 67, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("CaseBold", 14)
        c.drawString(M, H - 29, "CASECAP NBA QUANT")
        c.setFillColor(GOLD)
        c.setFont("CaseRegular", 7.5)
        c.drawString(M, H - 47, "NOVEMBER 4, 2026  /  UNPRICED SGP RESEARCH DRAFTS")
        game_block(c, ROWS[page * 2], 704, page * 2 + 1)
        c.setStrokeColor(LINE)
        c.line(M, 382, W - M, 382)
        game_block(c, ROWS[page * 2 + 1], 361, page * 2 + 2)
        footer(c, page + 2, total)
        c.showPage()
    if META:
        receipt_page(c, total, count, blocked)
    c.save()
    reader = PdfReader(OUT)
    assert len(reader.pages) == total
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert text.count("No ticket was constructed.") == blocked
    for game in ROWS:
        assert game["game"] in text
        for family in game["families"]:
            for leg in family["legs"]:
                assert leg["name"] in text
    print(str(OUT))
    print(f"Verified: {total} pages, 14 matchups, {count} tickets, {blocked} blocked families, all player names present.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.scan:
        data = json.loads(Path(args.scan).read_text(encoding="utf-8"))
        ROWS, META = data["games"], data["meta"]
    if args.output:
        OUT = Path(args.output)
    build()
