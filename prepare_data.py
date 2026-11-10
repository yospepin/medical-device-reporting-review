#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

GROUPS = ['0–30 days', '31–90 days', 'Over 90 days',
          'Unknown/invalid event timing']
FIELDS = ['mdr_report_key', 'report_number', 'date_received_raw',
          'date_of_event_raw', 'date_received', 'date_of_event',
          'receipt_month', 'lag_days', 'timing_group', 'timing_valid',
          'receipt_in_scope', 'event_date_status', 'receipt_date_status',
          'event_type', 'report_source_code', 'source_type_json',
          'product_problems_json', 'device_json', 'matching_devices_json',
          'device_count', 'matching_device_count', 'product_problem_count',
          'audit_flags_json', 'source_locations_json']
AUDIT_FIELDS = ['mdr_report_key', 'check', 'severity', 'field',
                'original_value_json', 'detail', 'source_location']


def js(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def text(value):
    return '' if value is None else str(value)


def parse_date(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, 'missing'
    # No guessing partial dates or replacing invalid values with invented days.
    value = str(value)
    if not re.fullmatch(r'\d{8}', value):
        return None, 'unparseable'
    try:
        return datetime.strptime(value, '%Y%m%d').date(), 'valid'
    except ValueError:
        return None, 'unparseable'


def write_csv(path, fields, rows):
    with path.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def transform(record, locations, year, product, audit):
    key = text(record.get('mdr_report_key'))
    flags = []

    def flag(check, severity, field, detail):
        flags.append(check)
        audit.append(dict(mdr_report_key=key, check=check, severity=severity,
                          field=field, original_value_json=js(record.get(field)),
                          detail=detail, source_location=locations[0]))

    received, receipt_status = parse_date(record.get('date_received'))
    event, event_status = parse_date(record.get('date_of_event'))
    for field, status in [('date_received', receipt_status), ('date_of_event', event_status)]:
        if status != 'valid':
            flag(f'{status}_{field}', 'error' if field == 'date_received' else 'warning',
                 field, 'Retained; no date imputation performed.')
    in_scope = received is not None and received.year == year
    if received and not in_scope:
        flag('receipt_outside_period', 'error', 'date_received',
             f'Retained for review; outside receipt year {year}.')
    lag = (received - event).days if received and event else None
    if lag is not None and lag < 0:
        flag('negative_lag', 'warning', 'date_of_event',
             'Event date is after FDA receipt date; lag retained but timing is invalid.')
    valid = in_scope and lag is not None and lag >= 0
    group = GROUPS[3]
    if valid:
        group = GROUPS[0] if lag <= 30 else GROUPS[1] if lag <= 90 else GROUPS[2]
    devices = record.get('device', [])
    problems = record.get('product_problems', [])
    if not isinstance(devices, list) or any(not isinstance(d, dict) for d in devices):
        flag('invalid_device_structure', 'error', 'device', 'Expected a list of device objects.')
        device_list = []
    else:
        device_list = devices
    matching = [d for d in device_list
                if text(d.get('device_report_product_code')).strip().upper() == product]
    if not matching:
        flag('no_matching_device', 'error', 'device', f'No device with product code {product}.')
    if len(device_list) > 1:
        flag('multiple_devices', 'info', 'device', 'Multiple devices preserved in one report row.')
    if not isinstance(problems, list):
        flag('invalid_problem_structure', 'warning', 'product_problems',
             'Original value preserved; expected an array.')
        problem_count = ''
    else:
        problem_count = len(problems)
        if not problems:
            flag('missing_product_problems', 'warning', 'product_problems', 'No listed product problems.')
        elif len(problems) > 1:
            flag('multiple_product_problems', 'info', 'product_problems',
                 'Multiple problems preserved in one report row.')
    return dict(
        mdr_report_key=key, report_number=text(record.get('report_number')),
        date_received_raw=text(record.get('date_received')),
        date_of_event_raw=text(record.get('date_of_event')),
        date_received=received.isoformat() if received else '',
        date_of_event=event.isoformat() if event else '',
        receipt_month=received.strftime('%Y-%m') if received else '',
        lag_days=lag if lag is not None else '', timing_group=group,
        timing_valid=int(valid), receipt_in_scope=int(in_scope),
        event_date_status=event_status, receipt_date_status=receipt_status,
        event_type=text(record.get('event_type')),
        report_source_code=text(record.get('report_source_code')),
        source_type_json=js(record.get('source_type', [])),
        product_problems_json=js(problems), device_json=js(devices),
        matching_devices_json=js(matching), device_count=len(device_list),
        matching_device_count=len(matching), product_problem_count=problem_count,
        audit_flags_json=js(flags), source_locations_json=js(locations))


def prepare(run_dir, output):
    manifest_path = run_dir / 'manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get('status') != 'complete':
        raise ValueError('Download manifest must have status complete.')
    year, product = int(manifest['receipt_year']), manifest['product_code']
    outputs = ['reports_clean.csv', 'data_quality_exceptions.csv', 'audit_summary.json']
    if any((output / name).exists() for name in outputs):
        raise ValueError('Output files already exist. Use --output with a new directory to preserve them.')
    output.mkdir(parents=True, exist_ok=True)
    audit, rows = [], []
    summary = dict(status='running', source_manifest=str(manifest_path.resolve()),
                   source_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                   prepared_at_utc=datetime.now(timezone.utc).isoformat(),
                   receipt_year=year, product_code=product)
    try:
        request_index = {r['file']: r for r in manifest['requests']}
        seen_files, records, locations = set(), {}, {}
        raw_count = identical = 0
        for window in manifest['windows']:
            if window['status'] == 'split_verified':
                continue
            if window['status'] != 'verified':
                raise ValueError('Unverified date window in completed manifest.')
            window_count = 0
            for filename in window['page_files']:
                if filename in seen_files:
                    raise ValueError(f'Page listed twice: {filename}')
                seen_files.add(filename)
                path = (run_dir / filename).resolve()
                if path.parent != run_dir.resolve():
                    raise ValueError('Page path is outside the selected run.')
                body = path.read_bytes()
                if hashlib.sha256(body).hexdigest() != request_index[filename]['sha256']:
                    raise ValueError(f'Checksum mismatch: {filename}; raw file changed.')
                page_records = json.loads(body)['results']
                window_count += len(page_records)
                for index, record in enumerate(page_records):
                    raw_count += 1
                    key = text(record.get('mdr_report_key'))
                    location = f'{filename}#results[{index}]'
                    if not key.strip():
                        raise ValueError(f'Missing report key: {location}')
                    if key in records:
                        same = record == records[key]
                        audit.append(dict(mdr_report_key=key,
                            check='identical_duplicate' if same else 'conflicting_duplicate',
                            severity='warning' if same else 'error', field='mdr_report_key',
                            original_value_json=js(key), source_location=location,
                            detail=f'First occurrence: {locations[key][0]}; ' +
                            ('identical full JSON record, collapsed.' if same else 'records differ; no automatic selection.')))
                        locations[key].append(location)
                        identical += int(same)
                    else:
                        records[key], locations[key] = record, [location]
            if window_count != window['downloaded_total'] or window_count != window['expected_total']:
                raise ValueError('Page counts do not match window totals.')
        summary.update(raw_records_loaded=raw_count, unique_report_keys=len(records),
                       identical_duplicate_rows=identical, page_files_read=len(seen_files))
        if any(a['check'] == 'conflicting_duplicate' for a in audit):
            raise ValueError('Conflicting duplicate keys found; inspect audit before proceeding.')
        if (raw_count != manifest['downloaded_total'] or
                len(records) != manifest['unique_report_keys'] or
                len(records) != manifest['expected_total']):
            raise ValueError('Loaded records do not reconcile to annual manifest totals.')
        for key in sorted(records):
            rows.append(transform(records[key], locations[key], year, product, audit))
        summary.update(report_rows=len(rows),
                       timing_group_counts={g: sum(r['timing_group'] == g for r in rows) for g in GROUPS},
                       reports_with_usable_timing=sum(r['timing_valid'] for r in rows),
                       reports_with_any_audit_entry=len({a['mdr_report_key'] for a in audit}),
                       audit_counts=dict(Counter(a['check'] for a in audit)),
                       status='needs_review' if any(a['severity'] == 'error' for a in audit) else 'complete')
        write_csv(output / 'reports_clean.csv', FIELDS, rows)
    except Exception as exc:
        summary.update(status='failed', error=str(exc))
        raise
    finally:
        write_csv(output / 'data_quality_exceptions.csv', AUDIT_FIELDS, audit)
        (output / 'audit_summary.json').write_text(js(summary), encoding='utf-8')
    return summary


def main():
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--output', type=Path, default=base / 'data' / 'processed')
    args = parser.parse_args()
    try:
        run_dir = args.run_dir
        if run_dir is None:
            candidates = []
            for path in sorted((base / 'data' / 'raw').glob('*/manifest.json')):
                m = json.loads(path.read_text(encoding='utf-8'))
                if m.get('status') == 'complete' and m.get('product_code') == 'LOX' and m.get('receipt_year') == 2025:
                    candidates.append(path.parent)
            if len(candidates) != 1:
                raise ValueError(f'Found {len(candidates)} completed LOX 2025 runs. '
                                 'Specify one with --run-dir; never combine extracts.')
            run_dir = candidates[0]
        print(f'Reading: {run_dir}')
        summary = prepare(run_dir, args.output)
        print(f'Status: {summary["status"]}\nReport rows: {summary["report_rows"]:,}')
        for group, count in summary['timing_group_counts'].items():
            print(f'  {group}: {count:,}')
        print('Audit checks:', summary['audit_counts'])
        print(f'Outputs: {args.output.resolve()}')
        if summary['status'] == 'needs_review':
            print('Resolve error-level audit entries before monthly analysis.')
            return 2
        return 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print(f'STOPPED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
