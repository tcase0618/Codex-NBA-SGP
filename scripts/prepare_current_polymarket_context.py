"""Append verified November 4 finals and refresh today's roster context."""
import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nba_quant.providers import parse_espn_summary
from nba_quant.schemas import Dataset
from nba_quant.store import Store
from nba_quant.slate import load_slate
from nba_quant.context import refresh_context
root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'output/review-2026-11-04/manifest.json').read_text())
teams={};players={};games=[]
for v in manifest['games'].values():
 source=v['source'];raw=Path(source['raw_path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==source['sha256']
 game,ts,ps,notes=parse_espn_summary(json.loads(raw),source)
 games.append(game);teams.update({t['id']:t for t in ts});players.update({p['id']:p for p in ps})
payload=Dataset.model_validate({'source':'ESPN verified November 4 final box-score archives','source_url':'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary','retrieved_at':max(g['known_at'] for g in games),'synthetic':False,'teams':list(teams.values()),'players':list(players.values()),'rotations':[],'games':games})
store=Store(root/'data/nba_quant.sqlite');did=store.import_dataset(payload)
slate=load_slate(refresh=True);context=refresh_context(slate)
result={'dataset_id':did,'games_appended_in_immutable_snapshot':len(games),'date':slate['date'],'slate_status':slate['status'],'games':[(g['id'],g['away']['name'],g['home']['name'],g['tipoff']) for g in slate['games']],'context_status':context['status'],'context_counts':context.get('counts')}
p=root/'output/polymarket/current-context-import.json';p.parent.mkdir(exist_ok=True,parents=True);p.write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
