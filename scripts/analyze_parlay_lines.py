"""Reconstruct every original selection, preserving its actual pregame evidence."""
import csv,hashlib,json,math,sqlite3,sys
from collections import Counter,defaultdict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nba_quant.schemas import utc
from nba_quant.store import digest
from nba_quant.line_diagnostics import exposure_decomposition
ROOT=Path(__file__).resolve().parents[1];D=ROOT/'output/review-2026-11-04'

def main():
 frozen=json.loads((D/'frozen-input.json').read_text());graded=json.loads((D/'graded-review.json').read_text());manifest=json.loads((D/'manifest.json').read_text())
 assert hashlib.sha256(Path(frozen['source_pdf']).read_bytes()).hexdigest()==frozen['pdf_sha256']
 db=sqlite3.connect(f'file:{ROOT / "data/nba_quant.sqlite"}?mode=ro',uri=True)
 entries=[];bundle_hashes={}
 for game in frozen['games']:
  matched=db.execute('select payload from draft_bundles where game_id=? and created_at=?',(game['id'],game['created_at'])).fetchall();assert len(matched)==1
  bundle=json.loads(matched[0][0]);bundle_hashes[game['id']]=digest(bundle)
  history={}
  for did in bundle['historical_dataset_ids']:
   data=json.loads(db.execute('select payload from datasets where id=?',(did,)).fetchone()[0]);assert digest(data)==did and data['synthetic'] is False
   for h in data['games']:
    if h['id'] not in history or utc(h['known_at'])>=utc(history[h['id']]['known_at']):history[h['id']]=h
  final=manifest['games'][game['id']];assert final['canonical_status']=='VALIDATED_FINAL'
  assert hashlib.sha256(Path(final['source']['raw_path']).read_bytes()).hexdigest()==final['source']['sha256']
  grade=next(g for g in graded['games'] if g['id']==game['id'])
  unique={}
  for family in bundle['families']:
   for leg in family['legs']:
    k=(leg['player_id'],leg['stat'],leg['side'],leg['line']);unique.setdefault(k,{'leg':leg,'sizes':[]})['sizes'].append(family['leg_count'])
  for k,sel in unique.items():
   leg=sel['leg'];pid,stat,side,line=k
   original=next(p for f in game['families'] for p in f['legs'] if (p['player_id'],p['stat'],p['side'],p['line'])==k)
   result=next(p for f in grade['families'] for p in f['legs'] if (p['player_id'],p['stat'],p['side'],p['line'])==k)
   assert original['name']==leg['player_name']
   samples=[]
   for gid in leg['sample_game_ids']:
    h=history[gid];assert utc(h['known_at'])<=utc(bundle['created_at']) and utc(h['completed_at'])<utc(bundle['created_at']) and utc(h['tipoff'])<utc(game['tipoff'])
    rows=[b for b in h['boxes'] if b['player_id']==pid and b['team_id']==leg['team_id'] and b['minutes']>0];assert len(rows)==1
    b=rows[0];v=b['oreb']+b['dreb'] if stat=='reb' else b[stat]
    samples.append({'game_id':gid,'minutes':b['minutes'],'value':v,'box':b})
   n=len(samples);mean=lambda key:sum(s['box'].get(key,0) for s in samples)/n
   values=[s['value'] for s in samples];avg=sum(values)/n;mins=sum(s['minutes'] for s in samples)/n
   hits=sum(v>=line if side=='at_least' else v>line if side=='over' else v<line for v in values)
   assert (n,hits)==(leg['descriptive']['games'],leg['descriptive']['hits'])
   actual=next(p for p in final['players'] if p['id']==pid and p['team_id']==leg['team_id'])
   actual_min=actual['stats'].get('minutes') if not actual['did_not_play'] else None
   decomp=exposure_decomposition(avg,mins,result['actual'],actual_min)
   shooting=None
   if stat=='fg3m' and not actual['did_not_play']:
    shooting=exposure_decomposition(avg,mean('fg3a'),result['actual'],actual['stats'].get('fg3a'))
   why=f"Historical {hits}/{n} ({hits/n:.0%}) at this threshold; highest qualifying {stat} ladder with >=80% frequency. Family balance: both teams, one player per leg, at most two of each stat."
   if actual['did_not_play']:why_result='Explicit DNP. Positive-minute-only screening did not model participation; no stat-rate conclusion or void settlement is inferred.'
   elif decomp is None:why_result='Final stat is available but complete minutes accounting is unavailable.'
   else:
    why_result=f"Actual {result['actual']} vs historical mean {avg:.2f}; actual minutes {actual_min:.2f} vs {mins:.2f}. Difference {decomp['total_difference']:+.2f} = minutes component {decomp['exposure_component']:+.2f} + realized-rate component {decomp['rate_component']:+.2f}."
    if shooting:why_result+=f" Three-point difference: attempt-volume component {shooting['exposure_component']:+.2f}, make-rate component {shooting['rate_component']:+.2f}."
    why_result+=' Accounting decomposition; coaching, defense, injury and shot-quality causes are unverified.'
   entries.append({'game_id':game['id'],'game':game['game'],'player_id':pid,'player':leg['player_name'],'team_id':leg['team_id'],'stat':stat,'side':side,'line':line,'families':sel['sizes'],'result':result['status'],'actual':result['actual'],'historical_games':n,'historical_hits':hits,'historical_hit_rate':hits/n,'historical_mean':avg,'historical_min':min(values),'historical_max':max(values),'historical_minutes':mins,'actual_minutes':actual_min,'naive_wilson_95':leg['descriptive']['interval_95'],'laplace_smoothed_frequency':(hits+1)/(n+2),'availability':leg['availability'],'injury_status':leg['injury_status'],'why_taken':why,'why_result':why_result,'minutes_rate_decomposition':decomp,'three_attempt_conversion_decomposition':shooting,'pregame_forecast_probability':leg.get('probability'),'offered_price':leg.get('offer'),'source_url':final['source_url'],'sample':samples,'bundle_id':bundle['id']})
 assert len(entries)==109 and sum(len(e['families']) for e in entries)==216
 resolved=[e for e in entries if e['result'] in ('PASS','FAIL')]
 groups={}
 for stat in sorted({e['stat'] for e in entries}):
  es=[e for e in resolved if e['stat']==stat];groups[stat]={'resolved':len(es),'passed':sum(e['result']=='PASS' for e in es),'historical_frequency_mean':sum(e['historical_hit_rate'] for e in es)/len(es),'actual_hit_rate':sum(e['result']=='PASS' for e in es)/len(es)}
 fails=[e for e in entries if e['result']=='FAIL'];components=Counter('minutes' if e['minutes_rate_decomposition']['exposure_component']<e['minutes_rate_decomposition']['rate_component'] else 'rate' for e in fails if e['minutes_rate_decomposition'])
 summary={'unique_selections':len(entries),'printed_leg_entries':216,'status_counts':dict(Counter(e['result'] for e in entries)),'sample_size_counts':dict(Counter(e['historical_games'] for e in entries)),'groups':groups,'miss_larger_negative_component':dict(components),'pregame_probabilities_available':sum(e['pregame_forecast_probability'] is not None for e in entries),'offered_prices_available':sum(e['offered_price'] is not None for e in entries),'all_availability_unverified':all(e['availability']=='UNVERIFIED' for e in entries),'mean_historical_frequency_resolved':sum(e['historical_hit_rate'] for e in resolved)/len(resolved),'realized_resolved_hit_rate':sum(e['result']=='PASS' for e in resolved)/len(resolved)}
 out={'summary':summary,'bundle_hashes':bundle_hashes,'source_as_of':graded['source_as_of'],'entries':entries,'limits':['No pregame point projections or forecast probabilities were stored for these drafts; historical means are reconstructed descriptive references, not frozen forecasts.','109 unique selections, not 216 independent bets; selections in same game and nested families are dependent.','Wilson intervals treat a fixed prespecified threshold; these thresholds were chosen on the same sample, so intervals have post-selection and small-sample limitations.','No causal defense, matchup, coaching or injury diagnosis from box scores alone.','One-night results cannot validate calibration or revised strategy performance.']}
 (D/'line-math-review.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
 fields=['game','player','stat','side','line','families','result','actual','historical_games','historical_hits','historical_hit_rate','historical_mean','historical_minutes','actual_minutes','laplace_smoothed_frequency','why_taken','why_result']
 with (D/'every-line-analysis.csv').open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(entries)
 lines=['# Every-line mathematical review - November 4, 2026','',json.dumps(summary,indent=2),'','## Limits']+['- '+s for s in out['limits']]
 for e in entries:
  lines+=['',f"## {e['game']} - {e['player']} {e['line']:g}+ {e['stat']}",'',f"**{e['result']}**, actual {e['actual']}; printed in {e['families']}-leg families.",'',e['why_taken'],'',e['why_result'],'',f"Pregame frequency interval (naive, post-selection): {e['naive_wilson_95']}; smoothed descriptive frequency {e['laplace_smoothed_frequency']:.3f}. No calibrated forecast probability was stored."]
 (D/'EVERY_LINE_ANALYSIS.md').write_text('\n'.join(lines),encoding='utf-8')
 print(json.dumps(summary,indent=2))
if __name__=='__main__':main()
