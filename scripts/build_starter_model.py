"""Train one fixed public-data starter recipe and build its redistributable files.

No raw market data, personal accounts, pickle or executable plugins are bundled.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from qwb import daily_data, experiments as ex, pipeline, runconfig, scenario
import pandas as pd


def build(workdir,output):
    workdir,output=Path(workdir).resolve(),Path(output).resolve()
    if output.exists():raise ValueError('Use a new distribution directory; do not replace a published version')
    snapshot=daily_data.current(workdir/'data/free_daily')
    source=ex.read(snapshot/'snapshot.json')
    if source['source']!='baostock':raise ValueError('The starter recipe requires the public BaoStock feed')
    codes=sorted(pd.read_csv(snapshot/'source.csv',usecols=['code']).code.unique())
    expected=sorted(pd.read_csv(ROOT/'src/qwb/static/free_daily_codes.csv').code)
    if codes!=expected:raise ValueError('Expected the declared 40-stock introductory universe')
    recipe={'name':'public40_lgbm_v1','task':scenario.task_card(5),
        'data':{'source':'snapshot','dir':str(snapshot)},
        'model':{'name':'lightgbm','num_boost_round':80},
        'train':{'universe':'all_ex_st','first_test_year':2022,'final_test_years':[2025,2026],
            'seeds':[0],'min_adv_yuan':600000,'portfolio':{'n_hold':10}},
        'output':'runs/build_public40_lgbm_v1'}
    resolved,report=runconfig.resolve(recipe,ROOT)
    if not report.ok:raise ValueError(str(report))
    summary=pipeline.run(resolved,workdir)
    run=Path(summary['output']);ex.verify(run)
    output.mkdir(parents=True)
    shutil.copytree(run/'final_model',output/'final_model')
    shutil.copy2(run/'predictions.parquet',output/'predictions.parquet')
    # Strip local filesystem paths, preserve all training/fold identities.
    meta=ex.read(output/'final_model/meta.json')
    meta['data']={'source':'snapshot','dir':'data/free_daily'}
    meta['starter_universe']=codes
    meta['starter_id']='public40_lgbm_v1'
    ex.write(output/'final_model/meta.json',meta)
    recipe['data']['dir']='data/free_daily'
    ex.write(output/'recipe.json',recipe)
    shutil.copy2(run/'folds.json',output/'folds.json')
    # Hash a canonical text representation on every build host. Git also leaves
    # distribution bytes untouched, so a checkout cannot invalidate this card.
    for path in output.rglob('*'):
        if path.suffix in {'.json','.txt'}:
            path.write_bytes(path.read_bytes().replace(b'\r\n',b'\n'))
    card={'schema':'starter_model_v1','id':'public40_lgbm_v1','name':'公开日线入门模型 · LightGBM',
        'version':1,'created_at':ex.now(),'source_url':'http://baostock.com/',
        'data_source':'BaoStock public daily OHLC, amount, ST status and adjustment factors',
        'data_start':source['start'],'data_end':source['end'],'snapshot_id':source['data_snapshot_id'],
        'codes':codes,'training_run_id':meta['run_id'],'training_code_hash':meta.get('code_hash'),
        'max_label_end':meta['max_label_end'],'n_train':meta['n_train'],
        'environment':{'python':summary['env']['python'],
            'packages':{k:summary['env']['packages'][k] for k in ('numpy','pandas','pyarrow','lightgbm')}},
        'training_sources':{name:ex.digest(ROOT/'src/qwb'/name) for name in ('daily.py','folds.py','pipeline.py','runconfig.py')},
        'weights_license':'AGPL-3.0-or-later','raw_data_bundled':False,
        'scope':'40 declared current stocks, introductory workflow only; not a point-in-time all-market universe',
        'limitations':['No claim of superior returns or unbiased historical universe selection',
            'Archived predictions are walk-forward; final model is for new data after the declared cutoff',
            'Download matching daily history before use; no user holdings are bundled'],
        'files':ex.inventory(output)}
    (output/'distribution.json').write_text(json.dumps(card,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'distribution':str(output),'training_run_id':meta['run_id'],'files':len(card['files'])},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser(__doc__);p.add_argument('--workdir',type=Path,default=ROOT)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args();build(args.workdir,args.output)
