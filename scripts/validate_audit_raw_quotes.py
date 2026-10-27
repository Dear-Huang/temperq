"""Recover requested audit dates from raw L2 archives without changing sources.

The legacy daily mean and a separately labelled continuous-session subset are
reported independently. Neither is a trade-time execution-cost measurement.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zlib

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_quotes(path, day, code):
    fields = ['万得代码', '自然日', '时间', '申买价1', '申卖价1', '申买量1', '申卖量1']
    # Vendor exports changed encoding; decode strictly instead of dropping bytes.
    with path.open('rb') as stream:
        prefix = stream.read(4)
    if prefix.startswith(b'\xef\xbb\xbf'):
        encoding = 'utf-8-sig'
        d = pd.read_csv(path, encoding=encoding, usecols=fields)
    else:
        encoding = 'gb18030'
        try:
            d = pd.read_csv(path, encoding=encoding, usecols=fields)
        except UnicodeDecodeError:
            encoding = 'utf-8'
            d = pd.read_csv(path, encoding=encoding, usecols=fields)
    raw_count = len(d)
    if d.empty:
        return {'status': 'empty', 'raw_rows': 0}, None, None
    if not d['万得代码'].astype(str).str.strip().eq(code).all():
        raise ValueError('Raw security identity does not match requested archive member')
    expected = day.replace('-', '')
    if not d['自然日'].astype(str).str.replace(r'\.0$', '', regex=True).eq(expected).all():
        raise ValueError('Raw observation date does not match requested date')
    for field in fields[2:]:
        d[field] = pd.to_numeric(d[field], errors='coerce')
    bid, ask = d['申买价1'], d['申卖价1']
    positive = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > 0)
    spread = (ask - bid) / ((ask + bid) / 2) * 10000
    # Legacy warehouse rounded the decimal spread mean to six decimal places.
    legacy_bp = round(float(spread[positive].mean()) / 10000, 6) * 10000 if positive.any() else None
    clock = d['时间']
    hour, minute, second = clock // 10000000, clock // 100000 % 100, clock // 1000 % 100
    valid_clock = (np.isfinite(clock) & clock.eq(np.floor(clock))
                   & hour.between(0, 23) & minute.between(0, 59) & second.between(0, 59))
    # Exclude the exact opening boundary and the closing auction from both markets.
    continuous = valid_clock & (((clock > 93000000) & (clock <= 113000000))
                                 | ((clock >= 130000000) & (clock < 145700000)))
    duplicate = d.duplicated(subset=fields)
    depth_ok = (np.isfinite(d['申买量1']) & np.isfinite(d['申卖量1'])
                & (d['申买量1'] > 0) & (d['申卖量1'] > 0))
    eligible = positive & (ask > bid) & depth_ok & continuous & ~duplicate
    bp = float(spread[eligible].mean()) if eligible.any() else None
    # Prices in the source are integer yuan*10000; 0.01 yuan is 100 raw units.
    ticks = (ask - bid) / 100
    def observation(value, count):
        if value is None or not np.isfinite(value) or not 0 < value <= 10000 or count < 10:
            return None
        return {'date': day, 'code': code, 'quoted_spread_bp': value,
                'sample_count': int(count), 'measurement_scope': 'daily_quote_mean'}
    quality = {'status': 'ok' if eligible.any() else 'no_eligible_continuous_quotes',
        'raw_rows': raw_count, 'encoding': encoding, 'positive_bid_ask_rows': int(positive.sum()),
        'nonpositive_or_missing_bid_ask_rows': int((~positive).sum()),
        'locked_rows': int((positive & (ask == bid)).sum()),
        'crossed_rows': int((positive & (ask < bid)).sum()),
        'invalid_clock_rows': int((~valid_clock).sum()),
        'duplicate_quote_rows': int(duplicate.sum()),
        'continuous_valid_unique_rows': int(eligible.sum()),
        'continuous_subtick_rows': int((eligible & (ticks < .98)).sum()),
        'legacy_full_spread_bp': legacy_bp,
        'continuous_full_spread_bp': bp,
        'continuous_median_full_spread_bp': float(spread[eligible].median()) if eligible.any() else None,
        'continuous_one_tick_share': float(np.isclose(ticks[eligible], 1).mean()) if eligible.any() else None,
        'source_sha256': digest(path)}
    return quality, observation(legacy_bp, int(positive.sum())), observation(bp, int(eligible.sum()))


def write_csv(path, rows, columns):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        w = csv.DictWriter(stream, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def select_members(listing, compact, codes):
    """Accept dated and flat vendor layouts, never arbitrary archive paths."""
    paths = [line[7:].replace('\\', '/') for line in listing.splitlines() if line.startswith('Path = ')]
    selected = {}
    for code in codes:
        allowed = {compact + '/' + code + '/行情.csv', code + '/行情.csv'}
        found = [p for p in paths if p in allowed]
        if len(found) > 1:
            raise ValueError('Ambiguous raw quote members; resolve duplicate archive entries')
        if found:
            selected[code] = found[0]
    return selected


def verified_cache(listing, members, compact, caches):
    entries = {}
    for block in listing.split('----------', 1)[-1].split('\n\n'):
        row = dict(line.split(' = ', 1) for line in block.splitlines() if ' = ' in line)
        if 'Path' in row:
            entries[row['Path'].replace('\\', '/')] = row
    found = {}
    for code, member in members.items():
        record = entries.get(member, {})
        if not record.get('CRC') or not record.get('Size'):
            return {}
        for cache in caches:
            p = cache / 'extracted' / compact / Path(member)
            if not p.is_file() or p.stat().st_size != int(record['Size']):
                continue
            crc = 0
            with p.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    crc = zlib.crc32(block, crc)
            if f'{crc & 0xffffffff:08X}' == record['CRC'].upper():
                found[code] = p
                break
    return found if len(found) == len(members) else {}


def main():
    from qwb.strategy_imports import SourceFile, normalize
    from qwb.strategy_audit import analyze
    from qwb.experiments import object_digest

    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--exports', type=Path, required=True)
    parser.add_argument('--archives', type=Path, required=True)
    parser.add_argument('--gaps', type=Path, required=True)
    parser.add_argument('--previous-quotes', type=Path, required=True)
    parser.add_argument('--seven-zip', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--opening-cash', type=float,
                        help='Explicit empty-opening declaration; otherwise use first exported close')
    parser.add_argument('--extracted-cache', type=Path, action='append', default=[],
                        help='Reuse only files whose size and CRC match this archive listing')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output must be a new directory')
    files = [SourceFile(p.name, p.read_bytes()) for p in sorted(args.exports.iterdir())
             if p.suffix.lower() in ('.csv', '.txt', '.py')]
    bundle = normalize(files, opening_cash=args.opening_cash)
    with args.gaps.open(encoding='utf-8-sig', newline='') as f:
        gaps = list(csv.DictReader(f))
    days = sorted({r['date'] for r in gaps})
    wanted = defaultdict(set)
    for fill in bundle['fills']:
        if fill['date'] in days:
            wanted[fill['date']].add(fill['code'])
    if set(days) != set(wanted):
        parser.error('Requested gap dates are not all in the imported fills')
    with args.previous_quotes.open(encoding='utf-8-sig', newline='') as f:
        previous = [{**r, 'sample_count': int(r['sample_count']),
                     'quoted_spread_bp': float(r['quoted_spread_bp'])} for r in csv.DictReader(f)]
    if len({(r['date'], r['code']) for r in previous}) != len(previous):
        parser.error('Previous quotes contain duplicate security dates')
    args.output.mkdir(parents=True)
    legacy, strict, evidence, sources = [], [], [], []
    for day in days:
        compact = day.replace('-', '')
        candidates = [args.archives / (compact + '.7z'), args.archives / (compact + '(1).7z')]
        # A newly downloaded duplicate takes precedence; record exactly which was used.
        archives = [p for p in candidates if p.is_file()]
        if not archives:
            raise FileNotFoundError('Requested archive is missing for ' + day)
        archive = max(archives, key=lambda p: p.stat().st_mtime_ns)
        codes = sorted(wanted[day])
        if not all(re.fullmatch(r'\d{6}\.(SH|SZ)', code) for code in codes):
            raise ValueError('Unsupported security identity in archive request')
        dest = args.output.resolve() / 'extracted' / compact
        dest.mkdir(parents=True)
        listing = subprocess.run([str(args.seven_zip), 'l', '-slt', '-sccUTF-8', str(archive)],
                                 capture_output=True, encoding='utf-8', errors='replace')
        if listing.returncode != 0:
            raise ValueError('Unable to list archive for ' + day)
        members = select_members(listing.stdout, compact, codes)
        if not members:
            raise ValueError('No requested quote members in archive for ' + day)
        cached = verified_cache(listing.stdout, members, compact, args.extracted_cache)
        if cached:
            proc = subprocess.CompletedProcess([], 0, 'Reused members after archive size/CRC verification.', '')
        else:
            command = [str(args.seven_zip), 'x', '-y', '-sccUTF-8', '-o' + str(dest), str(archive), *members.values()]
            proc = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace')
        (args.output / (compact + '-extract.log')).write_text(proc.stdout + '\n' + proc.stderr, encoding='utf-8')
        sources.append({'date': day, 'archive_name': archive.name, 'archive_bytes': archive.stat().st_size,
                        'archive_sha256': digest(archive), 'extraction_returncode': proc.returncode,
                        'reused_crc_verified_members': bool(cached)})
        if proc.returncode != 0:
            evidence.extend({'date': day, 'code': code, 'status': 'archive_extraction_failed'} for code in codes)
            print(json.dumps({'date': day, 'requested': len(codes), 'read': 0,
                              'extract_returncode': proc.returncode}), flush=True)
            continue
        count = 0
        for code in codes:
            path = cached.get(code) if cached else dest.joinpath(*members[code].split('/')) if code in members else None
            if path is None or not path.is_file():
                evidence.append({'date': day, 'code': code, 'status': 'member_missing_or_extract_failed'})
                continue
            quality, original, clean = read_quotes(path, day, code)
            evidence.append({'date': day, 'code': code, **quality})
            if original:
                legacy.append(original)
            if clean:
                strict.append(clean)
            count += 1
        print(json.dumps({'date': day, 'requested': len(codes), 'read': count,
                          'extract_returncode': proc.returncode}, ensure_ascii=True), flush=True)
    # Never silently retain legacy records for failed re-extractions on requested days.
    recovered = [r for r in previous if r['date'] not in days] + legacy
    recovered.sort(key=lambda r: (r['date'], r['code']))
    columns = ['date', 'code', 'quoted_spread_bp', 'sample_count', 'measurement_scope']
    write_csv(args.output / 'quoted_spreads.csv', recovered, columns)
    write_csv(args.output / 'continuous_subset.csv', strict, columns)
    def comparison(observations):
        bundle['quote_observations'] = [{**r, 'ref': {'source': 'quote_observations', 'row': i}}
                                        for i, r in enumerate(observations, 2)]
        tick = analyze(bundle)['modules']['tick_analysis']
        return {k: v for k, v in tick['calibration'].items() if k != 'matches'}
    recovered_keys = {(r['date'], r['code']) for r in legacy}
    strict_keys = {(r['date'], r['code']) for r in strict}
    gap_keys = {(r['date'], r['code']) for r in gaps}
    strict_pairs = [(r, next(x for x in legacy if x['date'] == r['date'] and x['code'] == r['code']))
                    for r in strict]
    summary = {'version': 1, 'export_digest': object_digest(normalize(files, opening_cash=args.opening_cash)),
        'opening_declaration': bundle['opening'],
        'previous_quote_sha256': digest(args.previous_quotes), 'gap_manifest_sha256': digest(args.gaps),
        'requested_days': days, 'requested_security_dates': sum(map(len, wanted.values())),
        'original_gap_security_dates': len(gap_keys), 'recovered_gap_security_dates': len(gap_keys & recovered_keys),
        'strict_valid_gap_security_dates': len(gap_keys & strict_keys),
        'remaining_gap_security_dates': [{'date': d, 'code': c} for d, c in sorted(gap_keys - recovered_keys)],
        'original_comparison': comparison(previous), 'recovered_legacy_comparison': comparison(recovered),
        'raw_legacy_subset_comparison': comparison(legacy), 'continuous_subset_comparison': comparison(strict),
        'quality_totals': {key: sum(r.get(key, 0) for r in evidence) for key in
            ['raw_rows', 'positive_bid_ask_rows', 'nonpositive_or_missing_bid_ask_rows', 'locked_rows',
             'crossed_rows', 'invalid_clock_rows', 'duplicate_quote_rows',
             'continuous_valid_unique_rows', 'continuous_subtick_rows']},
        'paired_security_dates': len(strict_pairs), 'sources': sources,
        'protocol': {'legacy': 'positive bid/ask; all sessions; decimal spread rounded to 6 places',
            'continuous': '09:30:00 < t <= 11:30:00 or 13:00:00 <= t < 14:57:00; positive two-sided prices and quantities; ask > bid; exact quote duplicates removed; equally weighted snapshots',
            'raw_price_scale': 10000, 'time_format': 'HHMMSSmmm exchange local time',
            'interpretation': 'Retrospective daily quoted spread; not actual fill costs, market impact or auction cost. Continuous subset is only requested dates, not the full strategy period. No model refit or sealed-period evaluation.'}}
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    (args.output / 'security_date_evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({key: summary[key] for key in ['original_gap_security_dates', 'recovered_gap_security_dates',
        'strict_valid_gap_security_dates', 'recovered_legacy_comparison', 'continuous_subset_comparison',
        'quality_totals']}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
