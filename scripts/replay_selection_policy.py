"""Development-only replay. Outcomes score revised selections, never select them."""
import json,sqlite3,sys
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nba_quant.construction import _select,VERSION
from nba_quant.store import digest
from grade_pdf_parlays import grade_family
root=Path(__file__).resolve().parents[1];d=root/'output/review-2026-11-04'
frozen=json.loads((d/'frozen-input.json').read_text());manifest=json.loads((d/'manifest.json').read_text())
db=sqlite3.connect(f'file:{root / "data/nba_quant.sqlite"}?mode=ro',uri=True)
rows=[];oldcounts=Counter();newcounts=Counter()
for game in frozen['games']:
 bundle=json.loads(db.execute('select payload from draft_bundles where game_id=? and created_at=?',(game['id'],game['created_at'])).fetchone()[0]);assert bundle['created_at']<game['tipoff']
 before=digest(bundle)
 for family in bundle['families']:
  size=family['leg_count'];legs=_select(bundle['candidates'],size,(bundle['provenance']['home_id'],bundle['provenance']['away_id']))
  # Outcomes are loaded by the grader only after both selection sets are fixed.
  def normalized(ls):return [{'name':p['player_name'],**p} for p in ls]
  box=manifest['games'][game['id']]
  old=grade_family({'size':size,'legs':normalized(family['legs'])},box)
  new=grade_family({'size':size,'legs':normalized(legs)},box)
  oldcounts[old['status']]+=1;newcounts[new['status']]+=1
  rows.append({'game':game['game'],'size':size,'original':old['status'],'revised_development_only':new['status'],'same_selections':{p['id'] for p in family['legs']}=={p['id'] for p in legs},'original_ids':[p['id'] for p in family['legs']],'revised_ids':[p['id'] for p in legs]})
 assert before==digest(bundle)
result={'version':VERSION,'scope':'Development replay on the already reviewed slate, not an independent holdout or evidence of improved accuracy. No parameters fitted to these outcomes. Frozen candidates only; no new availability or prices fabricated.','original_counts':dict(oldcounts),'revised_development_counts':dict(newcounts),'changed_families':sum(not r['same_selections'] for r in rows),'rows':rows}
(d/'selection-development-replay.json').write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))
