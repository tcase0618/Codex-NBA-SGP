"""Final-only report preserving all original draft thresholds."""
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo
import build_review_pdf as b
from reportlab.pdfgen import canvas
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[1]
D=ROOT/'output/review-2026-11-04'
r=json.loads((D/'graded-review.json').read_text())
assert r['games_final']==14
assert not any(f['status'] in ('PENDING','SECURED','UNRESOLVED') for g in r['games'] for f in g['families'])
b.R=r;b.ASOF=datetime.fromisoformat(r['source_as_of']).astimezone(ZoneInfo('America/New_York'));b.TOTAL=16
out=ROOT/'output/pdf/CASECAP_NBA_QUANT_Final_Win_Loss_2026-11-04.pdf'
c=canvas.Canvas(str(out),pagesize=(b.W,b.H));c.setTitle('CASECAP NBA QUANT | Final Win / Loss | November 4, 2026');c.setAuthor('CASECAP NBA QUANT')
b.header(c,'FINAL RESULTS / EXACT NOVEMBER 4 PRINTED PARLAYS',1)
b.write(c,'Final win / loss review',703,25,bold=True)
b.write(c,'All 14 games final. All 216 printed leg entries graded.',665,11,b.TEAL,True)
for i,(v,label) in enumerate([(16,'THRESHOLD WINS'),(23,'LOSSES'),(2,'DNP DEPENDENT'),(0,'STILL PENDING')]):
 x=b.M+134*i;c.setFillColor(b.NAVY);c.setFont('CaseBold',25);c.drawString(x,614,str(v));c.setFillColor(b.GRAY);c.setFont('CaseRegular',7);c.drawString(x,599,label)
b.write(c,'Resolved threshold hit rate: 16 / 39 = 41.0%. The two DNP-dependent tickets are excluded. One blocked 8-leg family was never constructed. These nested tickets share legs.',577,9,b.GRAY)
rows=[]
for g in r['games']:
 rows.append([g['game'],b.score(g)+(' / OT' if g['box']['period']>4 else ''),*[b.status_label(f['status']) for f in g['families']]])
b.grid(c,['MATCHUP (AWAY / HOME)','FINAL SCORE','3-LEG','5-LEG','8-LEG'],rows,523,[204,87,73,82,82],27,7.1)
b.write(c,'WIN means every printed threshold was achieved. DNP-dependent outcomes require the sportsbook\'s applicable void / repricing rules. No monetary return or account settlement is inferred.',101,8,b.GRAY)
b.write(c,'Frozen input: November 4, 6:14 PM rescan PDF. Final box scores retrieved '+b.ASOF.strftime('November %d, %I:%M:%S %p ET')+'.',65,7.5,b.GRAY)
c.showPage()
b.header(c,'FINAL BREAKDOWN / WHAT CHANGED FROM THE LIVE REVIEW',2)
b.write(c,'Final results by ticket size',703,23,bold=True)
rows=[]
for size in (3,5,8):
 counts=Counter(f['status'] for g in r['games'] for f in g['families'] if f['size']==size)
 rows.append([str(size)+' legs',counts['HIT'],counts['MISS'],counts['DNP_RULES_UNVERIFIED'],counts['BLOCKED']])
b.grid(c,['FAMILY','WIN','LOSS','DNP','BLOCKED'],rows,654,[110,95,95,114,114],31,10)
b.write(c,'The three previously unfinished games',501,15,bold=True)
y=470
for g in r['games'][-3:]:
 b.write(c,escape(g['game'])+' | '+b.score(g)+(' / OT' if g['box']['period']>4 else ''),y,12,b.TEAL,True)
 b.write(c,' / '.join(f"{f['size']}-leg: {f['status']}" for f in g['families']),y-24,10)
 unique={(p['player_id'],p['stat'],p['line']):p for f in g['families'] for p in f['legs'] if p['status']=='FAIL'}
 text='Missed: '+'; '.join(f"{p['name']} {p['actual']} vs {int(p['line'])}+ {b.LABEL[p['stat']]}" for p in unique.values())
 b.write(c,escape(text),y-46,9,b.GRAY);y-=100
b.write(c,'DNP treatment and audit scope',156,12,bold=True)
b.write(c,'Kelly Oubre Jr. did not play. Indiana 3- and 5-leg tickets met every other threshold and remain DNP-rule-dependent. Indiana 8-leg also missed Jaylen Wells 10+ PTS (actual 0). All 14 source boxes passed final reconciliation. Original selections and thresholds were preserved; 109 distinct selections: 86 passed, 22 failed, 1 DNP. Actual FanDuel combined prices and stakes are absent, so profit / ROI is not measurable.',133,8.5,b.GRAY)
c.showPage()
for n,g in enumerate(r['games'],3):b.game_page(c,g,n)
c.save()
pdf=PdfReader(out);assert len(pdf.pages)==16
for i,g in enumerate(r['games'],2):
 text=pdf.pages[i].extract_text()
 for f in g['families']:
  for p in f['legs']:assert p['name'] in text
lines=['# Final win / loss - November 4, 2026','',f"Source snapshot: {r['source_as_of']}",'','16 threshold wins; 23 losses; 2 DNP-rule-dependent; 0 pending. One blocked family was never constructed. Resolved hit rate 16/39 = 41.0%.','', '| Game | Final score | 3 legs | 5 legs | 8 legs |','|---|---|---|---|---|']
for g in r['games']:
 lines.append('| '+g['game']+' | '+b.score(g)+(' OT' if g['box']['period']>4 else '')+' | '+' | '.join(f['status'] for f in g['families'])+' |')
lines+=['','Kelly Oubre Jr. DNP: Indiana 3/5-leg require sportsbook rules; Indiana 8-leg also has a losing leg. No DNP void or repricing assumed. No ticket prices/stakes, so no monetary return calculated.','']
for g in r['games']:
 lines += ['## '+g['game'],'',g['box']['source_url'],'']
 for f in g['families']:
  lines += [f"### {f['size']}-leg: {f['status']}",'']
  for p in f['legs']:lines.append(f"- {p['name']}: {p['line']:g}+ {b.LABEL[p['stat']]}; actual {p['actual']}; {p['status']}")
(D/'FINAL_REVIEW.md').write_text('\n'.join(lines),encoding='utf-8')
print(out);print('Verified 16 pages and every original leg name present.')
