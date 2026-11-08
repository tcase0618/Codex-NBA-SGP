"""Acquire current public sources and construct required research families."""
import hashlib,json,sys
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nba_quant.providers import parse_espn_summary
from nba_quant.schemas import Dataset
from nba_quant.store import Store
from nba_quant.slate import load_slate
from nba_quant.context import refresh_context
from nba_quant.polymarket import refresh_polymarket_odds
from nba_quant.board import merged_history,build_game
ROOT=Path(__file__).resolve().parents[1];out=ROOT/'output/nightly-priority';out.mkdir(exist_ok=True,parents=True)
store=Store(ROOT/'data/nba_quant.sqlite')
manifest=json.loads((ROOT/'output/review-2026-11-05/manifest.json').read_text());games=[];teams={};players={}
for box in manifest['games'].values():
 receipt=box['source'];raw=Path(receipt['raw_path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==receipt['sha256']
 g,ts,ps,notes=parse_espn_summary(json.loads(raw),receipt);games.append(g);teams.update({t['id']:t for t in ts});players.update({p['id']:p for p in ps})
dataset=Dataset.model_validate({'source':'ESPN verified November 5 final box-score archives','source_url':'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary','retrieved_at':max(g['known_at'] for g in games),'synthetic':False,'teams':list(teams.values()),'players':list(players.values()),'rotations':[],'games':games})
did=store.import_dataset(dataset)
slate=load_slate(refresh=True);context=refresh_context(slate)
print(json.dumps({'stage':'current roster ready','date':slate['date'],'games':len(slate['games']),'history_added':len(games),'context':context['status']}),flush=True)
quotes=refresh_polymarket_odds(slate=slate)
print(json.dumps({'stage':'quotes acquired','status':quotes['status'],'coverage':quotes['coverage']}),flush=True)
history=merged_history(store);built=[];errors=[]
for game in slate['games']:
 if game.get('completed') or game.get('phase')!='pre':continue
 try:
  b=build_game(store,game,context,history,selection_mode='aggressive');built.append(b)
  print(json.dumps({'game':game['away']['name']+' @ '+game['home']['name'],'families':[(f['leg_count'],len(f['legs'])) for f in b['families']]}),flush=True)
 except ValueError as e:errors.append({'game_id':game['id'],'reason':str(e)})
report={'date':slate['date'],'created_at':datetime.now(timezone.utc).isoformat(),'imported_final_dataset_id':did,'history_games':len(history['games']),'source_status':quotes['status'],'coverage':quotes['coverage'],'games':built,'errors':errors,'limits':'Research drafts, no guaranteed win or quoted combined SGP payout; negative price-edge supports explicitly flagged.'}
p=out/f"nightly-{slate['date']}.json";p.write_text(json.dumps(report,indent=2));print('SAVED '+str(p),flush=True)
