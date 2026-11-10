#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import statistics
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

GROUPS = ['0–30 days','31–90 days','Over 90 days','Unknown/invalid event timing']
COLS = ['reports_0_30','reports_31_90','reports_over_90','reports_unknown_invalid']

def write_csv(path, rows, fields):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

def analyze(source, audit_path, output):
    raw=source.read_bytes()
    with source.open(encoding='utf-8-sig',newline='') as f: reports=list(csv.DictReader(f))
    audit=json.loads(audit_path.read_text())
    if audit['status']!='complete': raise ValueError('Resolve preparation audit errors first.')
    year=int(audit['receipt_year'])
    keys=[r['mdr_report_key'] for r in reports]
    if len(set(keys))!=len(keys) or any(not k.strip() for k in keys):
        raise ValueError('Duplicate/missing report keys.')
    if len(reports)!=audit['report_rows'] or len(reports)!=audit['unique_report_keys']:
        raise ValueError('Report count differs from audit.')
    for r in reports:
        received=date.fromisoformat(r['date_received'])
        if received.year!=year or r['receipt_month']!=received.strftime('%Y-%m'):
            raise ValueError('Receipt period mismatch.')
        if r['receipt_in_scope']!='1': raise ValueError('Out-of-scope report.')
        event=date.fromisoformat(r['date_of_event']) if r['date_of_event'] else None
        lag=(received-event).days if event else None
        if r['lag_days'] != (str(lag) if lag is not None else ''):
            raise ValueError('Calculated lag differs from source dates.')
        valid=lag is not None and lag>=0
        group=GROUPS[3] if not valid else GROUPS[0] if lag<=30 else GROUPS[1] if lag<=90 else GROUPS[2]
        if group!=r['timing_group'] or r['timing_valid']!=str(int(valid)):
            raise ValueError('Timing classification mismatch.')
    observed=Counter(r['timing_group'] for r in reports)
    if {g:observed[g] for g in GROUPS}!=audit['timing_group_counts']:
        raise ValueError('Timing counts differ from audit.')
    monthly=[]
    for m in range(1,13):
        month=f'{year}-{m:02d}'
        subset=[r for r in reports if r['receipt_month']==month]
        c=Counter(r['timing_group'] for r in subset)
        lags=[int(r['lag_days']) for r in subset if r['timing_valid']=='1']
        n=len(subset);valid=len(lags)
        row={'receipt_month':month,'total_reports':n,**{col:c[g] for col,g in zip(COLS,GROUPS)},
             'valid_timing_reports':valid,'usable_timing_share':valid/n if n else '',
             'over_90_share_valid':c[GROUPS[2]]/valid if valid else '',
             'median_lag_days_valid':statistics.median(lags) if valid else '',
             'month_over_month_change':n-monthly[-1]['total_reports'] if monthly else '',
             'month_over_month_pct_change':(n/monthly[-1]['total_reports']-1)
             if monthly and monthly[-1]['total_reports'] else ''}
        if sum(row[col] for col in COLS)!=n: raise ValueError('Monthly totals do not reconcile.')
        monthly.append(row)
    if sum(r['total_reports'] for r in monthly)!=len(reports): raise ValueError('Annual totals differ.')
    increases=[r for r in monthly[1:] if r['month_over_month_change']>0]
    chosen=max(increases,key=lambda r:r['month_over_month_change']) if increases else max(monthly,key=lambda r:r['total_reports'])
    index=monthly.index(chosen);previous=monthly[index-1] if index else None
    comparison=[]
    if previous:
        for col,g in zip(COLS,GROUPS):
            comparison.append({'timing_group':g,'previous_month':previous['receipt_month'],
                               'previous_count':previous[col],'selected_month':chosen['receipt_month'],
                               'selected_count':chosen[col],'count_change':chosen[col]-previous[col]})
        if sum(r['count_change'] for r in comparison)!=chosen['month_over_month_change']:
            raise ValueError('Change contributions do not reconcile.')
    summary={'status':'complete','created_at_utc':datetime.now(timezone.utc).isoformat(),
             'source_csv_sha256':hashlib.sha256(raw).hexdigest(),'source_audit_sha256':hashlib.sha256(audit_path.read_bytes()).hexdigest(),
             'receipt_year':year,'report_count':len(reports),'unique_report_keys':len(set(keys)),
             'selected_month':chosen['receipt_month'],
             'selection_rule':'Largest positive absolute monthly change; earliest month breaks ties.' if increases else 'Highest volume; no positive monthly change.',
             'selected_month_metrics':chosen,'previous_month_metrics':previous,
             'limitations':'Receipt-selected report records, not unique incidents. Timing composition does not establish causes, reporting compliance, device failure rates, or safety.'}
    output.mkdir(parents=True,exist_ok=True)
    names=['monthly_summary.csv','selected_month_comparison.csv','selected_month_reports.csv','analysis_summary.json']
    if any((output/n).exists() for n in names): raise ValueError('Outputs exist. Choose a new --output directory.')
    write_csv(output/names[0],monthly,list(monthly[0]))
    write_csv(output/names[1],comparison,['timing_group','previous_month','previous_count','selected_month','selected_count','count_change'])
    selected=[r for r in reports if r['receipt_month']==chosen['receipt_month']]
    write_csv(output/names[2],selected,list(reports[0]) if reports else ['mdr_report_key'])
    (output/names[3]).write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(f'Verified {len(reports):,} reports across 12 months. Selected: {chosen["receipt_month"]}')
    print(json.dumps(comparison,indent=2,ensure_ascii=False))
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,default=Path('data/processed/reports_clean.csv'))
    p.add_argument('--audit',type=Path,default=Path('data/processed/audit_summary.json'))
    p.add_argument('--output',type=Path,default=Path('outputs/monthly_analysis'))
    a=p.parse_args()
    analyze(a.input,a.audit,a.output)
