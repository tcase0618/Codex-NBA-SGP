"""Diagnose upward exceedances without selecting a replacement policy on winners."""
import csv,hashlib,json,math,sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nba_quant.construction import LADDERS
ROOT=Path(__file__).resolve().parents[1];D=ROOT/'output/review-2026-11-04'

def main():
 raw=(D/'line-math-review.json').read_bytes();source=json.loads(raw);rows=[]
 for e in source['entries']:
  if e['actual'] is None or e['actual']<1.25*e['line']:continue
  values=[s['value'] for s in e['sample']];n=len(values)
  ladder=[t for t in LADDERS[e['stat']] if t>e['line']]
  next_line=min(ladder) if ladder else None
  next_hits=sum(v>=next_line for v in values) if next_line is not None else None
  assert not any(sum(v>=t for v in values)/n>=.8 for t in ladder)
  dense=max(t for t in range(1,max(values)+1) if sum(v>=t for v in values)/n>=.8)
  row={k:e[k] for k in ['game','player','stat','line','actual','families','historical_mean','historical_games','historical_hits','historical_minutes','actual_minutes','source_url']}
  row.update({'percent_above_line':100*(e['actual']/e['line']-1),'next_ladder_line':next_line,'next_line_historical_hits':next_hits,'next_line_historical_rate':next_hits/n if next_hits is not None else None,'hits_required':math.ceil(.8*n-1e-12),'highest_integer_empirical_floor':dense,'dense_threshold_is_verified_offer':False,'actual_minus_historical_mean':e['actual']-e['historical_mean'],'minutes_component':e['minutes_rate_decomposition']['exposure_component'] if e['minutes_rate_decomposition'] else None,'rate_component':e['minutes_rate_decomposition']['rate_component'] if e['minutes_rate_decomposition'] else None,'why_low':f"Chosen threshold passed {e['historical_hits']}/{n}; next ladder {next_line}+ passed only {next_hits}/{n}, below the required {math.ceil(.8*n-1e-12)}/{n}. Preset statistic-specific ladder and 80% in-sample floor policy; no fitted matchup projection or verified offered-price optimization.",'sample_values':values})
  rows.append(row)
 rows.sort(key=lambda r:(r['stat']!='pts',-r['percent_above_line'],r['player']))
 pts=[r for r in rows if r['stat']=='pts'];allpts=[e for e in source['entries'] if e['stat']=='pts']
 summary={'points_exceedances':len(pts),'total_points_selections':len(allpts),'all_stat_exceedances':len(rows),'points_next_ladder_qualified_original_screen':sum(r['next_line_historical_rate']>=.8 for r in pts),'points_with_integer_floor_above_chosen':sum(r['highest_integer_empirical_floor']>r['line'] for r in pts),'points_actual_below_historical_mean':sum(r['actual_minus_historical_mean']<0 for r in pts),'all_points_uniform_raise_development_only':[{ 'raise':s,'hit':sum(e['actual']>=e['line']+s for e in allpts),'total':len(allpts)} for s in [0,5,10]]}
 result={'definition':'actual >= 1.25 * selected line; points primary, other count-stat thresholds included in detail','source_sha256':hashlib.sha256(raw).hexdigest(),'summary':summary,'rows':rows,'limits':['Original selections are analytical thresholds, not verified FanDuel offers. No price/EV conclusion is possible.','80% history frequency is not a forecast probability. These 18 winners are selected after outcomes; no replacement rule is fitted or validated here.','Highest integer empirical floor diagnoses coarse ladder spacing only; it is not an offered line or a recommendation.','Actual minutes/rate differences are accounting components, not verified coaching/defense/injury causes.']}
 (D/'overclear-analysis.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
 with (D/'overclear-analysis.csv').open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 lines=['# Lines exceeded by at least 25%','',result['definition'],'',f"18/27 points selections met the filter; {len(rows)} selections across all count stats. Original thresholds and outcomes remain unchanged.",'','The policy selected the highest five-point points ladder threshold clearing >=80% in its own small historical sample. This was a lower-tail empirical floor, not a mean forecast, matchup-based upside model or offered-price optimization. No higher available ladder threshold passed the original rule for any of these 18 players.','', '| Player | Line | Actual | Above line | Prior mean | Next ladder history | Integer floor* |','|---|---:|---:|---:|---:|---|---:|']
 for r in pts:lines.append(f"| {r['player']} | {r['line']:g}+ | {r['actual']:g} | {r['percent_above_line']:.1f}% | {r['historical_mean']:.2f} | {r['next_ladder_line']:g}+: {r['next_line_historical_hits']}/{r['historical_games']} | {r['highest_integer_empirical_floor']} |")
 lines += ['', '*Integer floor is the highest integer threshold clearing the same sample rule, a grid-spacing diagnostic only. It is not a verified sportsbook offer. 17/18 were held below that integer floor by the five-point grid.','', 'Five of these 18 players scored below their own historical sample mean despite clearing the selected line by at least 25%. Raising every one of the full 27 points selections by five would have changed 22/27 hits to 16/27; raising by ten gives 9/27. These are same-slate sensitivity counts, not a forecast or profitability test.','', 'The appropriate future extension is to retain separate floor and upside analyses, evaluate the actual offered alternate ladder, model minutes and scoring-attempt distributions, and compare calibrated probability against offered prices. Choosing higher lines because these winners overshot would introduce hindsight selection.','', '## Every exceedance']
 for r in rows:
  lines+=['',f"### {r['game']} - {r['player']} {r['line']:g}+ {r['stat']} -> {r['actual']}",'',r['why_low'],'',f"Historical values: {r['sample_values']}. Historical mean {r['historical_mean']:.2f}; actual minutes {r['actual_minutes']} vs prior mean {r['historical_minutes']:.2f}. Count difference from mean: minutes {r['minutes_component']:+.3f}, realized-rate {r['rate_component']:+.3f}.",'',r['source_url']]
 lines+=['','## Limits']+['- '+l for l in result['limits']]
 (D/'OVERCLEAR_ANALYSIS.md').write_text('\n'.join(lines),encoding='utf-8')
 print(json.dumps(summary,indent=2))
if __name__=='__main__':main()
