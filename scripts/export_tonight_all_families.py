"""Export every saved current 3/5/8 family; no selection or price changes."""
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from pypdf import PdfReader
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "output/nightly-priority/tonight-all-sizes-2026-11-06-under-policy.json"
OUT = ROOT / "output/pdf/CASECAP_NBA_QUANT_Tonight_3_5_8_Corrected_2026-11-06.pdf"
FOLDER = ROOT / "output/nightly-priority"
W, H, M = 612, 792, 42
NAVY, TEAL, GRAY, PALE = [HexColor(s) for s in ("#101E2A", "#087D77", "#536372", "#F1F5F6")]
EASTERN = ZoneInfo("America/New_York")
STATS = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM"}


def text(c, value, x, y, width=528, size=9, color=NAVY, bold=False):
    style = ParagraphStyle("p", fontName="CaseBold" if bold else "CaseRegular",
                           fontSize=size, leading=size*1.3, textColor=color)
    p = Paragraph(escape(str(value)), style)
    _, height = p.wrap(width, 1000)
    p.drawOn(c, x, y-height)
    return height


def title(c, subtitle, page, total, asof):
    c.setFillColor(NAVY); c.rect(0, H-64, W, 64, fill=1, stroke=0)
    text(c, "CASECAP NBA QUANT", M, H-20, size=15, color=white, bold=True)
    text(c, subtitle, M, H-43, size=7.5, color=white)
    c.setStrokeColor(HexColor("#DCE3E8")); c.line(M, 40, W-M, 40)
    text(c, "NOV 6, 2026 | CAPTURED " + asof.strftime("%I:%M:%S %p ET"), M, 32, size=7, color=GRAY)
    c.setFont("CaseRegular", 7); c.setFillColor(GRAY); c.drawRightString(W-M, 24, f"{page:02d} / {total:02d}")


def table(c, headers, rows, y, widths, height=21, size=8.5):
    c.setFillColor(NAVY); c.rect(M, y-22, sum(widths), 22, fill=1, stroke=0)
    x=M
    for h,w in zip(headers,widths):text(c,h,x+7,y-6,w-12,7,white,True);x+=w
    y-=22
    for i,row in enumerate(rows):
        c.setFillColor(PALE if i%2==0 else white); c.rect(M,y-height,sum(widths),height,fill=1,stroke=0)
        x=M
        for value,w in zip(row,widths):text(c,value,x+7,y-5,w-12,size);x+=w
        y-=height
    return y


def selection(leg):
    side = f"{leg['line']:g}+" if leg['side']=="at_least" else f"{leg['side'].title()} {leg['line']:g}"
    return side + " " + STATS[leg["stat"]]


def negative(leg):
    ev=leg.get("aggressive_evidence",{})
    edge=ev.get("proxy_return_after_entry_fee")
    if edge is None:edge=ev.get("proxy_return_gross")
    return edge is not None and edge<=0


def main():
    raw=SOURCE.read_bytes();r=json.loads(raw)
    # Read the already-acquired dated slate, without a network refresh.
    import sys
    sys.path.insert(0,str(ROOT))
    from nba_quant.slate import load_slate
    from nba_quant.line_policy import line_policy_reason
    slate=load_slate(day=r["date"]);names={g["id"]:g for g in slate["games"]}
    asof=datetime.fromisoformat(r["created_at"]).astimezone(EASTERN)
    for name,path in [("CaseRegular","C:/Windows/Fonts/arial.ttf"),("CaseBold","C:/Windows/Fonts/arialbd.ttf")]:
        pdfmetrics.registerFont(TTFont(name,path))
    OUT.parent.mkdir(exist_ok=True,parents=True)
    c=canvas.Canvas(str(OUT),pagesize=(W,H));c.setTitle("CASECAP NBA QUANT | Tonight 3/5/8 | November 6, 2026")
    c.setAuthor("CASECAP NBA QUANT")
    counts={n:sum(any(f["leg_count"]==n and len(f["legs"])==n for f in b["families"]) for b in r["games"]) for n in (3,5,8)}
    total=len(r["games"])+1
    title(c,"TONIGHT / COMPLETE SAVED 3-, 5- AND 8-LEG DRAFT LIST",1,total,asof)
    text(c,"Corrected 3 / 5 / 8-leg drafts",M,704,size=24,bold=True)
    text(c,f"{len(r['games'])} games | {counts[3]} three-leg | {counts[5]} five-leg | {counts[8]} eight-leg",M,663,size=12,color=TEAL,bold=True)
    text(c,"Rule: unders use source-identified standard lines only; alternate lines are over-only. This edition supersedes the earlier PDF. Individual Polymarket prices are not a quoted combined SGP payout.",M,629,size=9,color=GRAY)
    overview=[];markdown=["# CASECAP NBA QUANT - Tonight's 3/5/8-leg drafts","",f"November 6, 2026. Captured {asof.strftime('%I:%M:%S %p ET')}. {counts[3]} three-leg, {counts[5]} five-leg, {counts[8]} eight-leg drafts.","","American odds are captured gross individual Polymarket BUY asks, not combined parlay quotes. Prices can change. These are uncalibrated research drafts; final game-specific availability still requires review. Support* marks a nonpositive model-estimated price edge; no positive-edge or win guarantee is asserted.","","| Game | 3 legs | 5 legs | 8 legs |","|---|---|---|---|"]
    export=[]
    for b in r["games"]:
        g=names[b["game_id"]];assert b["tipoff"]==g["tipoff"]
        name=g["away"]["name"]+" @ "+g["home"]["name"]
        flags=["READY" if next(f for f in b["families"] if f["leg_count"]==n)["legs"] else "UNAVAILABLE" for n in (3,5,8)]
        overview.append([name,*flags]);markdown.append("| "+name+" | "+" | ".join(flags)+" |")
    table(c,["GAME","3 LEGS","5 LEGS","8 LEGS"],overview,568,[288,80,80,80],30,8)
    text(c,"Each size is constructed separately. Three-leg coverage alternatives are labelled where the strict screen did not produce a ticket. No under is inferred to be standard from its price or ladder position. One draft per available size/game is shown.",M,241,size=9)
    text(c,"* Support legs may lack a positive estimated price edge. Negative estimates and anchor shortfalls are disclosed. A higher estimated hit probability does not establish a profitable price.",M,186,size=9,color=GRAY)
    text(c,"Quotes have individual observation times and expire from the live feed after five minutes. No live ticket submission, stake instruction or guaranteed result is included. Full leg/source records accompany this PDF in the CSV and JSON export.",M,129,size=9,color=GRAY)
    c.showPage()
    for page,b in enumerate(r["games"],2):
        g=names[b["game_id"]];name=g["away"]["name"]+" @ "+g["home"]["name"]
        title(c,"EXACT SAVED SELECTIONS / POLYMARKET INDIVIDUAL ODDS",page,total,asof)
        text(c,name,M,706,size=18,bold=True)
        tip=datetime.fromisoformat(b["tipoff"]).astimezone(EASTERN)
        text(c,"Tipoff "+tip.strftime("%I:%M %p ET")+" | Individual gross prices; combined SGP quote unavailable",M,678,size=8.5,color=GRAY)
        markdown.extend(["","## "+name,"","Tipoff "+tip.strftime("%I:%M %p ET")+".",""])
        y=646
        for f in b["families"]:
            n=f["leg_count"];label=f"{n}-LEG DRAFT"+(" / COVERAGE ALTERNATIVE" if f.get("is_coverage_alternative") else "")
            text(c,label,M,y,size=12,color=TEAL,bold=True);y-=23
            markdown.extend([f"### {n} legs",""])
            if f.get("is_coverage_alternative"):markdown.extend(["Coverage alternative: the strict three-leg price-edge screen did not produce a family; this does not assert a positive edge.",""])
            if not f["legs"]:
                reason=f["block_reasons"][0];text(c,"Unavailable: "+reason,M,y,size=9,color=GRAY)
                markdown.extend(["Unavailable: "+reason,""]);y-=56;continue
            assert len(f["legs"])==n
            rows=[];markdown.extend(["| Player | Selection | American | Role |","|---|---|---:|---|"])
            for leg in f["legs"]:
                o=leg["offer"];assert o["sportsbook"]=="polymarket" and o["executable"] is True
                assert line_policy_reason(leg["side"],o) is None
                assert o["quote_basis"]=="ORDERBOOK_BEST_ASK"
                role=leg.get("family_role","aggressive");role+= "*" if negative(leg) else ""
                price=f"{o['american_odds']:+.0f}"
                rows.append([leg["player_name"],selection(leg),price,role])
                markdown.append(f"| {leg['player_name']} | {selection(leg)} | {price} | {role} |")
                export.append({"game_id":b["game_id"],"game":name,"tipoff":b["tipoff"],"family_size":n,"family_id":f["id"],"player_id":leg["player_id"],"player":leg["player_name"],"stat":leg["stat"],"side":leg["side"],"line":leg["line"],"american_odds":o["american_odds"],"role":role,"quote_observed_at":o["observed_at"],"source_url":o["source_url"],"source_sha256":o["source_sha256"]})
            y=table(c,["PLAYER","EXACT SELECTION","AMERICAN","ROLE"],rows,y,[201,143,84,100],19,8.8)
            opt=f.get("optimization",{});note=f"{opt.get('anchors_selected',0)} anchors / {n-opt.get('anchors_selected',0)} supports; {sum(negative(p) for p in f['legs'])} nonpositive price-edge estimates." if opt else "Aggressive research selection; no calibrated joint probability."
            text(c,note,M,y-7,size=7.7,color=GRAY);y-=36
            markdown.extend(["",note,""])
        assert y>=60, (name,y)
        text(c,"* Nonpositive estimated price edge. Probabilities are uncalibrated; availability is not independently confirmed.",M,85,size=7,color=GRAY)
        text(c,"Source: "+next((p['offer']['source_url'] for f in b['families'] for p in f['legs']),"No complete source-bound draft"),M,66,size=6.8,color=GRAY)
        c.showPage()
    c.save()
    reader=PdfReader(OUT);assert len(reader.pages)==total
    assert sum(len(f["legs"]) for b in r["games"] for f in b["families"])==len(export)
    for index,b in enumerate(r["games"],1):
        content=reader.pages[index].extract_text()
        for f in b["families"]:
            for leg in f["legs"]:assert leg["player_name"] in content
    markdown.insert(4,"CORRECTED RULE: standard-source unders only; over alternatives allowed. Unclassified unders excluded. Supersedes the earlier list.")
    (FOLDER/"TONIGHT_3_5_8_CORRECTED.md").write_text("\n".join(markdown),encoding="utf-8")
    if export:
        with (FOLDER/"tonight-3-5-8-corrected.csv").open("w",newline="",encoding="utf-8-sig") as stream:
            writer=csv.DictWriter(stream,fieldnames=list(export[0]));writer.writeheader();writer.writerows(export)
    receipt={"pdf":str(OUT),"pages":total,"counts":counts,"printed_leg_entries":len(export),"input_sha256":hashlib.sha256(raw).hexdigest(),"captured_at":r["created_at"],"visual_review":"pending"}
    receipt["under_entries"] = sum(x["side"]=="under" for x in export)
    receipt["line_rule"] = "Source-identified standard unders only; alternate lines over-only"
    (FOLDER/"all-family-corrected-verification.json").write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt,indent=2))


if __name__=="__main__":main()
