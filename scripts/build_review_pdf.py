"""Complete branded PDF audit of the exact November 4 frozen draft book."""
import collections
import json
import math
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib.colors import HexColor, white
from reportlab.pdfgen import canvas
from pypdf import PdfReader

from build_parlay_pdf import paragraph, NAVY, TEAL, GOLD, GRAY, LINE, PALE, W, H, M

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "output/review-2026-11-04"
OUT = ROOT / "output/pdf/CASECAP_NBA_QUANT_Parlay_Review_2026-11-04.pdf"
R = json.loads((DATA / "graded-review-live.json").read_text(encoding="utf-8"))
ASOF = datetime.fromisoformat(R["source_as_of"]).astimezone(ZoneInfo("America/New_York"))
LABEL = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM"}
COLOR = {"PASS": TEAL, "HIT": TEAL, "SECURED": TEAL, "FAIL": HexColor("#AF3C3C"), "MISS": HexColor("#AF3C3C"),
         "PENDING": HexColor("#946514"), "DNP": HexColor("#946514"), "DNP_RULES_UNVERIFIED": HexColor("#946514")}
TOTAL = 18


def header(c, subtitle, page):
    c.setFillColor(NAVY); c.rect(0, H - 65, W, 65, fill=1, stroke=0)
    c.setFillColor(white); c.setFont("CaseBold", 14); c.drawString(M, H - 28, "CASECAP NBA QUANT")
    c.setFillColor(GOLD); c.setFont("CaseRegular", 7.5); c.drawString(M, H - 46, subtitle)
    c.setStrokeColor(LINE); c.line(M, 39, W - M, 39)
    c.setFillColor(GRAY); c.setFont("CaseRegular", 7)
    c.drawString(M, 25, "NOV 4 GAMES | REVIEW AS OF " + ASOF.strftime("NOV %d, %I:%M:%S %p ET"))
    c.drawRightString(W - M, 25, f"{page:02d} / {TOTAL:02d}")


def write(c, text, y, size=9, color=NAVY, bold=False, width=None, x=M):
    return paragraph(c, text, x, y, width or W - M - x, size, size * 1.4, color, bold)


def score(g):
    b = g["box"]
    home = next(t for t in b["teams"].values() if t["home_away"] == "home")
    away = next(t for t in b["teams"].values() if t["home_away"] == "away")
    return f"{away['score']} - {home['score']}"


def state(g):
    return "FINAL" + (" / OT" if g["box"]["period"] > 4 else "") if g["box"]["completed"] else f"Q{g['box']['period']} {g['box']['clock']}"


def status_label(s):
    return {"DNP_RULES_UNVERIFIED": "DNP / rules unknown", "SECURED": "All thresholds met*", "BLOCKED": "Not constructed"}.get(s, s)


def probability(f):
    if f["status"] == "PENDING" and f.get("conditional_probability_bounds"):
        b = f["conditional_probability_bounds"]
        return f"~{100*b['low']:.0f}-{100*b['high']:.0f}% scenario range"
    if f["status"] == "SECURED": return "100% already met*"
    if f["status"] in {"HIT", "MISS"}: return "Observed final result"
    return "Not measurable"


def grid(c, heads, rows, y, widths, row_height=25, font=8):
    xs = [M]
    for w in widths: xs.append(xs[-1] + w)
    c.setFillColor(NAVY); c.rect(M, y-24, sum(widths), 24, fill=1, stroke=0)
    for i, h in enumerate(heads): paragraph(c, escape(h), xs[i]+7, y-7, widths[i]-12, 7, 9, white, True)
    cursor = y - 24
    for n, row in enumerate(rows):
        c.setFillColor(PALE if n % 2 == 0 else white); c.rect(M, cursor-row_height, sum(widths), row_height, fill=1, stroke=0)
        for i, cell in enumerate(row):
            text, color = cell if isinstance(cell, tuple) else (str(cell), NAVY)
            paragraph(c, escape(str(text)), xs[i]+7, cursor-7, widths[i]-12, font, font*1.25, color)
        cursor -= row_height
    return cursor


def overview(c):
    header(c, "POSTGAME + LIVE THRESHOLD AUDIT / FROZEN 6:14 PM PDF", 1)
    write(c, "Full parlay review", 703, 25, bold=True)
    write(c, "11 final games. 3 live games. Every printed selection checked.", 665, 11, TEAL, True)
    for i, (n, label) in enumerate([(12, "FINAL HITS"), (18, "FINAL MISSES"), (2, "DNP DEPENDENT"), (9, "LIVE TICKETS")]):
        x = M + 134*i; c.setFillColor(NAVY); c.setFont("CaseBold", 25); c.drawString(x, 614, str(n))
        c.setFillColor(GRAY); c.setFont("CaseRegular", 7); c.drawString(x, 599, label)
    write(c, "Snapshot: " + ASOF.strftime("%B %d, %Y, %I:%M:%S %p Eastern") +
          ". The PDF's 41 constructed drafts are preserved; one blocked family was not a ticket. This reviews thresholds, not sportsbook settlement or account P&amp;L.", 577, 9, GRAY)
    rows = []
    for g in R["games"]:
        names = g["game"].replace("Philadelphia 76ers", "76ers").replace("Portland Trail Blazers", "Trail Blazers").replace("Minnesota Timberwolves", "Timberwolves").replace("Oklahoma City Thunder", "Thunder").replace("Los Angeles Lakers", "Lakers")
        rows.append([names, score(g)+" / "+state(g)] + [(status_label(f["status"]), COLOR.get(f["status"], GRAY)) for f in g["families"]])
    grid(c, ["MATCHUP (AWAY @ HOME)", "SCORE / STATE", "3-LEG", "5-LEG", "8-LEG"], rows, 523, [181, 100, 82, 82, 83], 27, 7.2)
    write(c, "*Live: all thresholds reached, subject to official stat corrections. Not a final sportsbook win. DNP: no void or repricing outcome assumed.", 101, 8, GRAY)
    write(c, "Review result: COMPLETE threshold audit; NEEDS REVISION for actionable betting claims. Joint calibration, actual offered prices and settlement rules remain unavailable.", 73, 8, GRAY)
    c.showPage()


def findings(c):
    header(c, "WHAT HIT, WHAT MISSED, AND WHAT THE RESULT MEANS", 2)
    write(c, "Final-game findings", 702, 22, bold=True)
    write(c, "12 of 30 resolved final tickets hit every threshold (40%). Two additional final tickets depend on DNP rules. These nested drafts are correlated and do not represent 30 independent trials or a validated betting strategy.", 666, 10)
    rows = []
    for size in (3,5,8):
        fs = [f for g in R["games"] if g["box"]["completed"] for f in g["families"] if f["size"]==size]
        ct = collections.Counter(f["status"] for f in fs)
        rows.append([f"{size}-leg", ct["HIT"], ct["MISS"], ct["DNP_RULES_UNVERIFIED"], ct["BLOCKED"]])
    grid(c, ["FAMILY", "HIT", "MISS", "DNP RULES", "BLOCKED"], rows, 600, [150,90,90,105,93], 24, 9)
    write(c, "All 16 distinct missed selections", 481, 15, bold=True)
    distinct = {}
    for g in R["games"]:
        for f in g["families"]:
            for p in f["legs"]:
                if p["status"]=="FAIL": distinct[(g["id"],p["player_id"],p["stat"],p["line"])]=p
    rows = [[p["name"], f"{int(p['line'])}+ {LABEL[p['stat']]}", p["actual"], int(p["line"]-p["actual"])] for p in distinct.values()]
    grid(c, ["PLAYER", "SELECTED TARGET", "FINAL", "SHORT BY"], rows, 456, [210,150,80,88], 18, 8)
    write(c, "DNP: Kelly Oubre Jr. / 2+ rebounds", 119, 11, HexColor("#946514"), True)
    write(c, "Indiana-Memphis: the 3- and 5-leg tickets otherwise hit their thresholds, but Oubre was explicitly DNP. The 8-leg also missed Jaylen Wells 10+ points (0). No void, loss or repriced payout is inferred from the DNP alone.", 98, 8.5, GRAY)
    c.showPage()


def live(c):
    header(c, "LIVE GAME SNAPSHOT / CONDITIONAL PROBABILITY LIMITS", 3)
    write(c, "Still live at the source snapshot", 702, 22, bold=True)
    write(c, "Ranges below are rough, uncalibrated scenarios - NOT confidence intervals or fitted SGP odds. Playing time, new injuries and correlated outcomes can change them materially.", 665, 9, GRAY)
    y = 617
    for g in R["games"]:
        if g["box"]["completed"]: continue
        write(c, escape(g["game"]), y, 13, bold=True)
        write(c, score(g)+" | "+state(g)+" | "+datetime.fromisoformat(g["box"]["source"]["retrieved_at"]).astimezone(ZoneInfo("America/New_York")).strftime("%I:%M:%S %p ET"), y-23, 9, TEAL, True)
        rows = [[f"{f['size']}-leg", status_label(f["status"]), probability(f)] for f in g["families"]]
        grid(c,["TICKET","CURRENT STATE","CONDITIONAL HIT RANGE"],rows,y-43,[90,190,248],21,8.5)
        pending={}
        for f in g["families"]:
            for p in f["legs"]:
                if p["status"]=="PENDING": pending[(p["player_id"],p["stat"])]=p
        desc="; ".join(f"{p['name']}: {p['actual']}/{int(p['line'])} {LABEL[p['stat']]} (needs {int(p['remaining'])})" for p in pending.values())
        write(c, escape("Outstanding: "+desc),y-139,8.3,GRAY)
        y-=190
    write(c,"Method: prior-rate Gamma-Poisson leg scenarios, with remaining minutes varied; dependence bounded using Frechet inequalities. No independent leg-probability product is used. See methods page for assumptions.", 59, 7.5, GRAY)
    c.showPage()


def game_page(c, g, number):
    header(c, "EXACT PRINTED LEGS / FINAL OR CURRENT BOX-SCORE COMPARISON", number)
    write(c, escape(g["game"]), 702, 21, bold=True)
    write(c, score(g)+"  |  "+state(g), 668, 14, TEAL, True)
    observed=datetime.fromisoformat(g["box"]["source"]["retrieved_at"]).astimezone(ZoneInfo("America/New_York"))
    write(c,"Observed "+observed.strftime("Nov %d, %I:%M:%S %p ET")+" | source event "+g["id"]+" | "+g["box"]["canonical_status"],643,8,GRAY)
    rows=[]
    for f in g["families"]:
        denominator=len(f["legs"])
        passed=f.get("passed_legs",0)
        rows.append([f"{f['size']}-leg",(status_label(f["status"]),COLOR.get(f["status"],GRAY)),f"{passed}/{denominator} thresholds met" if denominator else "No ticket", probability(f)])
    grid(c,["TICKET","OUTCOME / STATE","LEG PROGRESS","HIT ASSESSMENT"],rows,616,[65,150,135,178],28,8.5)
    write(c,"Every distinct printed leg",490,15,bold=True)
    write(c,"Ticket membership shows which printed 3-, 5- and 8-leg families contain the selection.",468,8,GRAY)
    unique={}
    for f in g["families"]:
        for p in f["legs"]:
            k=(p["player_id"],p["stat"],p["line"])
            if k not in unique: unique[k]={"leg":p,"families":[]}
            unique[k]["families"].append(str(f["size"]))
    rows=[]
    for item in unique.values():
        p=item["leg"]
        actual="DNP" if p["status"]=="DNP" else "Missing" if p["actual"] is None else str(p["actual"])
        rows.append([p["name"],f"{int(p['line'])}+ {LABEL[p['stat']]}",actual,(p["status"],COLOR.get(p["status"],GRAY))," / ".join(item["families"]),"-" if p["remaining"] is None else str(int(p["remaining"]))])
    grid(c,["PLAYER","TARGET","BOX","LEG STATE","TICKETS","NEEDS"],rows,449,[163,93,44,70,91,67],30,8.4)
    y=449-24-30*len(rows)-20
    if g["box"]["completed"]:
        fail=[p for item in unique.values() if (p:=item["leg"])["status"]=="FAIL"]
        dnp=[p for item in unique.values() if (p:=item["leg"])["status"]=="DNP"]
        if fail:
            write(c,"Missed thresholds: "+escape("; ".join(f"{p['name']} {p['actual']} vs {int(p['line'])}+ {LABEL[p['stat']]}" for p in fail)),y,9,HexColor("#AF3C3C")); y-=53
        if dnp:
            write(c,"DNP: "+escape(", ".join(p["name"] for p in dnp))+". No sportsbook void or repricing rule is assumed.",y,9,HexColor("#946514")); y-=35
    else:
        estimates=[item["leg"] for item in unique.values() if item["leg"].get("live_estimate")]
        write(c,"Pending-leg scenario estimates (uncalibrated)",y,10,bold=True); y-=20
        for p in estimates:
            e=p["live_estimate"]
            text=f"{p['name']} / {LABEL[p['stat']]}: ~{e['conditional_low']*100:.0f}-{e['conditional_high']*100:.0f}% (central minutes: {e['conditional_central']*100:.0f}%; {e['historical_games']} prior games)."
            write(c,escape(text),y,8,GRAY);y-=15
        write(c,"Live PASS means the threshold is already reached, subject to corrections. It is not a final settled win.",y-8,8,GRAY)
    source=g["box"]["source_url"]
    write(c,'<link href="'+escape(source)+'" color="#087D77">'+escape(source)+'</link>',91,8,TEAL)
    write(c,"Source SHA256: "+g["box"]["source"]["sha256"],73,6.6,GRAY)
    write(c,"This is a threshold audit of an unpriced draft. No stake, offered SGP payout or monetary result was supplied.",57,7.3,GRAY)
    c.showPage()


def methods(c):
    header(c,"METHODS, AUDIT COVERAGE AND MATERIAL LIMITS",18)
    write(c,"What was checked",703,22,bold=True)
    write(c,"The actual nine-page PDF was independently compared to its exact frozen JSON and immutable construction bundles: 14 games, 41 constructed tickets, one blocked family and all 216 leg entries. No leg, threshold or eligibility rule was changed after results.",666,10)
    grid(c,["ARTIFACT QUALITY","OBSERVED DEFECTS","ASSESSMENT"],[
        ["Review completeness","0 / 14 games","All games and printed tickets covered; 3 games still live at snapshot."],
        ["Analytical clarity","0 / 41 tickets","Final results, DNPs and provisional live states separated."],
        ["Visual / interaction","N/A","Static PDF; all output pages rendered for visual inspection."]],605,[128,104,296],42,8)
    grid(c,["ANALYTICAL CHECK","OBSERVED DEFECTS","ASSESSMENT"],[
        ["Source / joins","0 / 14 games","Exact event and player IDs checked; all 11 finals reconciled."],
        ["Value arithmetic","0 / 216 entries","Independent raw-response comparison matched actuals and grades."],
        ["Cross-artifact match","0 / 216 entries","PDF names / targets / stats match frozen source bundles."],
        ["Live hit probability","6 / 6 pending tickets","No calibrated joint model; conditional ranges are assumption-based."],
        ["Bet settlement / ROI","41 / 41 tickets","Exact offered prices, stakes and book rules were not provided."]],426,[128,104,296],41,8)
    write(c,"Counts are scoped checks, not a forecast-accuracy percentage. No review defects were found in the threshold grading; probability and settlement gaps prevent actionable betting claims.",177,8,GRAY)
    write(c,"Live method and scope",141,10,bold=True)
    write(c,"Prior rate uses only completed current-team games known before the PDF scan (latest 10; minimum 3). Gamma-Poisson updating uses current counts/minutes and 20 prior pseudo-minutes. Remaining minutes vary 0.5x/1x/1.5x the historical minutes/48 share of remaining regulation clock, capped at that clock. No injuries, role changes, overtime or tactical adjustment is predicted. Frechet leg-dependence bounds are not confidence intervals; no fitted ticket point probability is available.",121,7.7,GRAY)
    c.showPage()


def main():
    OUT.parent.mkdir(parents=True,exist_ok=True)
    c=canvas.Canvas(str(OUT),pagesize=(W,H));c.setTitle("CASECAP NBA QUANT | Full Parlay Review | November 4, 2026")
    c.setAuthor("CASECAP NBA QUANT");c.setSubject("Exact threshold audit with final box scores and provisional live scenarios")
    overview(c);findings(c);live(c)
    for n,g in enumerate(R["games"],4):game_page(c,g,n)
    methods(c);c.save()
    r=PdfReader(OUT);assert len(r.pages)==TOTAL
    full="\n".join(p.extract_text() for p in r.pages)
    for g in R["games"]:
        assert g["game"] in full
        page=r.pages[3+R["games"].index(g)].extract_text()
        for f in g["families"]:
            for p in f["legs"]:assert p["name"] in page
    print(str(OUT));print("Verified 18 pages, 14 game reviews, every printed leg present.")


if __name__=="__main__":main()
