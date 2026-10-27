"""Synthetic source-quality checks, not empirical cost-model validation."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest


def raw_module():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'validate_audit_raw_quotes.py'
    spec = importlib.util.spec_from_file_location('audit_raw_quotes', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def quotes():
    # Ten distinct continuous snapshots and one exact duplicate. The remaining
    # observations exercise auction boundaries, locked/crossed/one-sided books.
    rows = [{'万得代码': '600000.SH', '自然日': 20240529, '时间': 93100000 + i * 1000,
             '申买价1': 100000, '申卖价1': 100100, '申买量1': 100, '申卖量1': 100}
            for i in range(10)]
    rows.append(dict(rows[0]))
    for clock, bid, ask, quantity in [(93000000, 100000, 101000, 100),
        (145700000, 100000, 101000, 100), (93200000, 100000, 100000, 100),
        (93300000, 100100, 100000, 100), (93400000, 0, 100000, 100),
        (93500000, 100000, 100100, 0), (93060000, 100000, 101000, 100)]:
        rows.append({'万得代码': '600000.SH', '自然日': 20240529, '时间': clock,
                     '申买价1': bid, '申卖价1': ask, '申买量1': quantity, '申卖量1': quantity})
    return pd.DataFrame(rows)


def test_raw_continuous_quotes_exclude_invalid_books_duplicates_and_auctions(tmp_path):
    module = raw_module()
    path = tmp_path / 'quotes.csv'
    quotes().to_csv(path, encoding='gb18030', index=False)
    quality, legacy, strict = module.read_quotes(path, '2024-05-29', '600000.SH')
    assert quality['raw_rows'] == 18
    assert quality['duplicate_quote_rows'] == 1
    assert quality['locked_rows'] == quality['crossed_rows'] == quality['invalid_clock_rows'] == 1
    assert quality['continuous_valid_unique_rows'] == 10
    assert quality['continuous_subtick_rows'] == 0
    assert strict['sample_count'] == 10
    assert strict['quoted_spread_bp'] == pytest.approx(100 / 100050 * 10000)
    assert legacy['quoted_spread_bp'] != pytest.approx(strict['quoted_spread_bp'])
    assert quality['continuous_one_tick_share'] == 1.


def test_raw_source_identity_and_observation_count_are_checked(tmp_path):
    module = raw_module()
    path = tmp_path / 'quotes.csv'
    quotes().iloc[:9].to_csv(path, encoding='gb18030', index=False)
    quality, legacy, strict = module.read_quotes(path, '2024-05-29', '600000.SH')
    assert quality['continuous_valid_unique_rows'] == 9
    assert strict is None and legacy is None
    with pytest.raises(ValueError, match='date'):
        module.read_quotes(path, '2024-05-30', '600000.SH')
    with pytest.raises(ValueError, match='identity'):
        module.read_quotes(path, '2024-05-29', '000001.SZ')


def test_archive_layouts_are_resolved_without_ambiguous_or_unrelated_paths():
    module = raw_module()
    listing = 'Path = 20240529\\600000.SH\\行情.csv\nPath = 000001.SZ\\行情.csv\nPath = ../600000.SH/行情.csv'
    assert module.select_members(listing, '20240529', ['600000.SH', '000001.SZ']) == {
        '600000.SH': '20240529/600000.SH/行情.csv', '000001.SZ': '000001.SZ/行情.csv'}
    with pytest.raises(ValueError, match='Ambiguous'):
        module.select_members(listing + '\nPath = 600000.SH/行情.csv', '20240529', ['600000.SH'])


def test_utf8_vendor_quotes_are_decoded_without_loss(tmp_path):
    module = raw_module()
    path = tmp_path / 'quotes.csv'
    quotes().to_csv(path, encoding='utf-8-sig', index=False)
    quality, _, strict = module.read_quotes(path, '2024-05-29', '600000.SH')
    assert quality['encoding'] == 'utf-8-sig'
    assert strict['sample_count'] == 10


def test_extracted_cache_requires_matching_archive_crc_and_size(tmp_path):
    import zlib
    module = raw_module()
    p = tmp_path / 'extracted' / '20240529' / '600000.SH' / '行情.csv'
    p.parent.mkdir(parents=True)
    payload = b'quote-data'
    p.write_bytes(payload)
    member = '600000.SH/行情.csv'
    listing = f'----------\nPath = {member}\nSize = {len(payload)}\nCRC = {zlib.crc32(payload):08X}\n'
    assert module.verified_cache(listing, {'600000.SH': member}, '20240529', [tmp_path]) == {'600000.SH': p}
    p.write_bytes(b'bad-quotes')
    assert module.verified_cache(listing, {'600000.SH': member}, '20240529', [tmp_path]) == {}
