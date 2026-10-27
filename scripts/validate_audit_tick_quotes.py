"""Read-only same-date L2 comparison; never reads dates outside imported fills.

Exports a sixth optional audit CSV and a provenance summary to a new directory.
The supported private warehouse layout is date=YYYYMMDD/part.parquet; columns
are trade_date, wind_code, 相对价差_均 (decimal full spread), 快照数.
Daily means cannot certify opening/closing-auction execution costs.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    from qwb import experiments as ex
    from qwb.strategy_imports import SourceFile, normalize
    from qwb.strategy_audit import analyze
    import pyarrow.parquet as pq

    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--exports', type=Path, required=True)
    parser.add_argument('--l2-daily', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--opening-cash', type=float)
    parser.add_argument('--min-snapshots', type=int, default=10)
    args = parser.parse_args()
    if args.min_snapshots < 1:
        parser.error('min-snapshots must be positive')
    if args.output.exists():
        parser.error('output must be a new directory')
    files = [SourceFile(p.name, p.read_bytes()) for p in sorted(args.exports.iterdir())
             if p.suffix.lower() in ('.csv', '.txt', '.py')]
    bundle = normalize(files, opening_cash=args.opening_cash)
    export_digest = ex.object_digest(bundle)
    wanted = defaultdict(set)
    for fill in bundle['fills']:
        wanted[fill['date']].add(fill['code'])
    records, sources, missing_dates, unsupported_dates = [], [], [], []
    rejected, no_row = 0, 0
    for day, codes in sorted(wanted.items()):
        source = args.l2_daily / ('date='+day.replace('-', '')) / 'part.parquet'
        if not source.exists():
            missing_dates.append(day)
            continue
        parquet = pq.ParquetFile(source)
        columns_needed = ['trade_date', 'wind_code', '相对价差_均', '快照数']
        if not set(columns_needed) <= set(parquet.schema_arrow.names):
            unsupported_dates.append(day)
            continue
        table = parquet.read(columns=columns_needed).to_pylist()
        found = set()
        for row in table:
            code = row['wind_code']
            if code not in codes or str(row['trade_date']) != day:
                continue
            if code in found:
                raise ValueError('Duplicate L2 security-date; reconcile source before use')
            found.add(code)
            spread, count = row['相对价差_均'], row['快照数']
            if (spread is None or count is None or not math.isfinite(spread) or not math.isfinite(count)
                    or not 0 < spread <= 1 or count < args.min_snapshots or count != int(count)):
                rejected += 1
                continue
            records.append({'date': day, 'code': code, 'quoted_spread_bp': spread * 10000,
                            'sample_count': int(count), 'measurement_scope': 'daily_quote_mean'})
        no_row += len(codes - found)
        sources.append({'date': day, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
    args.output.mkdir(parents=True)
    dest = args.output / 'quoted_spreads.csv'
    columns = ['date', 'code', 'quoted_spread_bp', 'sample_count', 'measurement_scope']
    with dest.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader(); writer.writerows(records)
    bundle['quote_observations'] = [{**r, 'ref': {'source': 'quote_observations', 'row': i}} for i, r in enumerate(records, 2)]
    result = analyze(bundle)
    summary = {'version': 1, 'export_input_digest': export_digest,
        'quote_csv_sha256': hashlib.sha256(dest.read_bytes()).hexdigest(),
        'l2_sources': sources, 'missing_partition_dates': missing_dates,
        'unsupported_schema_dates': unsupported_dates,
        'no_matching_security_date': no_row, 'quality_rejected_security_dates': rejected,
        'minimum_snapshots': args.min_snapshots,
        'tick_all_fills': result['modules']['tick_analysis']['all_exported_fills'],
        'calibration': result['modules']['tick_analysis']['calibration'],
        'interpretation': 'Retrospective daily quote comparison only; source may mix sessions and includes no trade-time/auction execution evidence. No model is fitted or evaluated.'}
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary['calibration'].items() if k != 'matches'}, ensure_ascii=True))


if __name__ == '__main__':
    main()
