#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

PATTERNS = {
    'registry_reference': r'\bREGISTRY\b|\bCATH\s*PCI\b',
    'cathpci_reference': r'\bCATH\s*PCI\b',
    'event_year_only': r'\bEVENT\s+DATE\b[^.!?]{0,180}\b(?:YEAR\s+ONLY|ONLY\s+(?:THE\s+)?YEAR)\b',
    'january_1_placeholder': r'\bJANUARY\s+0?1(?:ST)?\b[^.!?]{0,100}\b(?:USED|CAPTURE|ASSIGN|ENTER|SUBSTITUT)',
    'possible_duplicate': r'\bPOSSIBLE\s+DUPLICATE\b',
    'prior_reporting_uncertain': r'\b(?:CANNOT|CAN\s+NOT|COULD\s+NOT)\s+BE\s+DETERMINED\b[^.!?]{0,160}\bPREVIOUSLY\s+REPORTED\b',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def screen(sections):
    """Search each section independently; keep original evidence and section IDs."""
    evidence = []
    for i, section in enumerate(sections, 1):
        if not isinstance(section, dict):
            raise ValueError('Narrative section must be an object')
        text = re.sub(r'\s+', ' ', str(section.get('text') or '')).strip()
        for flag, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text, re.I):
                evidence.append({'flag': flag, 'section': i,
                                 'section_type': section.get('text_type_code', ''),
                                 'excerpt': text[max(0, match.start()-100):match.end()+180]})
    return {flag: int(any(e['flag'] == flag for e in evidence)) for flag in PATTERNS}, evidence


def choose_run(root, supplied):
    if supplied:
        return supplied.resolve()
    choices = []
    for path in (root/'data/raw').glob('*/manifest.json'):
        m = json.loads(path.read_text())
        if (m.get('status'), m.get('product_code'), m.get('receipt_year')) == ('complete', 'LOX', 2025):
            choices.append(path.parent.resolve())
    if len(choices) != 1:
        raise ValueError(f'Found {len(choices)} completed LOX 2025 runs; specify --run-dir.')
    return choices[0]


def extract(run, rows):
    manifest_path = run/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get('status'), manifest.get('product_code'), manifest.get('receipt_year')) != ('complete', 'LOX', 2025):
        raise ValueError('A completed LOX 2025 extraction is required.')
    requests = {r['file']: r for r in manifest['requests']}
    records, seen = {}, set()
    for window in manifest['windows']:
        if window['status'] == 'split_verified':
            continue
        if window['status'] != 'verified':
            raise ValueError('Unverified extraction window')
        if window['end'] < '2025-05-01' or window['start'] > '2025-05-31':
            continue
        for name in window['page_files']:
            path = (run/name).resolve()
            if path.parent != run or name in seen:
                raise ValueError('Unsafe or repeated page path')
            seen.add(name)
            sha = digest(path)
            if sha != requests[name]['sha256']:
                raise ValueError(f'Checksum mismatch: {name}')
            for index, record in enumerate(json.loads(path.read_bytes())['results']):
                received = str(record.get('date_received') or '')
                if not '20250501' <= received <= '20250531':
                    continue
                key = str(record.get('mdr_report_key') or '')
                if not key or key in records:
                    raise ValueError(f'Missing or repeated raw report key: {key}')
                if not any(d.get('device_report_product_code') == 'LOX' for d in record.get('device', [])):
                    raise ValueError(f'Raw record lacks LOX device: {key}')
                records[key] = (record, {'page': name, 'result_index': index, 'sha256': sha})
    expected = {r['mdr_report_key'] for r in rows}
    if set(records) != expected:
        raise ValueError(f'Raw/clean key mismatch. Missing={sorted(expected-set(records))}; extra={sorted(set(records)-expected)}')
    return records, digest(manifest_path)


def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run(args):
    if args.output.exists():
        raise ValueError('Output folder exists; choose a new --output folder to preserve review notes.')
    with args.input.open(encoding='utf-8-sig', newline='') as f:
        all_rows = list(csv.DictReader(f))
    if len({r['mdr_report_key'] for r in all_rows}) != len(all_rows):
        raise ValueError('Duplicate clean report keys')
    may = [r for r in all_rows if r['receipt_month'] == '2025-05']
    if len(may) != args.expected_count:
        raise ValueError(f'Expected {args.expected_count} May reports, found {len(may)}. Investigate before changing expected count.')
    raw, manifest_sha = extract(choose_run(args.project, args.run_dir), may)
    result, evidence, packets = [], [], {}
    for row in sorted(may, key=lambda r: r['mdr_report_key']):
        key = row['mdr_report_key']
        record, origin = raw[key]
        for clean, original in [('date_received_raw', 'date_received'), ('date_of_event_raw', 'date_of_event')]:
            if row[clean] != str(record.get(original) or ''):
                raise ValueError(f'Raw/clean date mismatch: {key}')
        sections = record.get('mdr_text') or []
        if not isinstance(sections, list):
            raise ValueError(f'Invalid narrative structure: {key}')
        flags, matches = screen(sections)
        manufacturers = sorted({d.get('manufacturer_d_name', '').upper() for d in record['device'] if d.get('device_report_product_code') == 'LOX'})
        item = {'mdr_report_key': key, 'report_number': row['report_number'],
                'manufacturer': '; '.join(manufacturers),
                'boston_scientific_device': int(any('BOSTON SCIENTIFIC' in n for n in manufacturers)),
                'receipt_date': row['date_received'], 'event_date': row['date_of_event'],
                'recorded_lag_days': row['lag_days'], 'timing_group': row['timing_group'],
                'has_narrative': int(any(str(s.get('text') or '').strip() for s in sections)),
                'event_date_is_2024_01_01': int(row['date_of_event_raw'] == '20240101'),
                **flags, 'minimum_lag_if_year_only': '', 'over_90_robust_if_year_only': '',
                'source_page': origin['page'], 'source_result_index': origin['result_index']}
        # Bounds are conditional on confirming the phrase flag and structured event year.
        if flags['event_year_only'] and row['date_of_event']:
            year = date.fromisoformat(row['date_of_event']).year
            lower = (date.fromisoformat(row['date_received']) - date(year, 12, 31)).days
            item['minimum_lag_if_year_only'] = lower
            item['over_90_robust_if_year_only'] = int(lower > 90)
        result.append(item)
        evidence.extend({'mdr_report_key': key, **e} for e in matches)
        packets[key] = {'report': item, 'evidence': matches, 'narrative_sections': sections, 'provenance': origin}
    counts = {flag: sum(r[flag] for r in result) for flag in PATTERNS}
    intersections = {
        'registry_and_year_only': sum(r['registry_reference'] and r['event_year_only'] for r in result),
        'bsc_registry': sum(r['boston_scientific_device'] and r['registry_reference'] for r in result),
        'bsc_year_only': sum(r['boston_scientific_device'] and r['event_year_only'] for r in result),
        'older_registry': sum(r['timing_group'] == 'Over 90 days' and r['registry_reference'] for r in result),
    }
    # Fixed hash ordering, reproducible and independent of narrative meaning.
    # Include flagged + unflagged examples, then ensure each detected flag has an example.
    order = sorted(result, key=lambda r: hashlib.sha256(('may-review-v1:'+r['mdr_report_key']).encode()).hexdigest())
    flagged = [r for r in order if any(r[f] for f in PATTERNS)]
    unflagged = [r for r in order if not any(r[f] for f in PATTERNS)]
    chosen = {r['mdr_report_key']: r for r in flagged[:4] + unflagged[:4]}
    for flag in PATTERNS:
        if counts[flag] and not any(r[flag] for r in chosen.values()):
            r = next(r for r in order if r[flag])
            chosen[r['mdr_report_key']] = r
    summary = {'status': 'Automated screening complete; manual validation pending',
               'created_at_utc': datetime.now(timezone.utc).isoformat(),
               'clean_csv_sha256': digest(args.input), 'manifest_sha256': manifest_sha,
               'month': '2025-05', 'report_count': len(result),
               'with_narrative': sum(r['has_narrative'] for r in result),
               'flag_counts': counts, 'intersections': intersections,
               'boston_scientific_reports': sum(r['boston_scientific_device'] for r in result),
               'recorded_january_1_2024': sum(r['event_date_is_2024_01_01'] for r in result),
               'rules': PATTERNS, 'manual_review_keys': list(chosen),
               'limitations': ['Phrase flags require human confirmation; absence of a phrase is not evidence of absence.',
                              'Counts overlap; each flag counts distinct report keys.',
                              'Narratives reflect the saved snapshot and may contain later supplements.',
                              'Possible duplicates are retained; distinct report keys are not necessarily distinct incidents.',
                              'Date bounds are conditional on correct event-year interpretation.',
                              'No causality, failure incidence, compliance, or backlog conclusion is made.']}
    md = ['# May narrative screening — manual validation packet', '',
          'Review the full sections, confirm or reject each flag, and note missed wording. This small, deliberately mixed selection is not representative and does not estimate classification accuracy.', '']
    for key in chosen:
        packet = packets[key]
        md += [f'## Report {key}', '', json.dumps(packet['report'], ensure_ascii=False), '',
               '**Review:** [Confirm/reject each flag; note missed flags, date precision, and unresolved questions.]', '']
        for i, s in enumerate(packet['narrative_sections'], 1):
            md += [f'### Section {i}: {s.get("text_type_code", "unspecified")}', '', str(s.get('text') or '[No text]'), '']
    args.output.mkdir(parents=True)
    write_csv(args.output/'may_screening.csv', result, list(result[0]))
    write_csv(args.output/'screening_evidence.csv', evidence, ['mdr_report_key','flag','section','section_type','excerpt'])
    (args.output/'screening_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    (args.output/'manual_review.md').write_text('\n'.join(md), encoding='utf-8')
    (args.output/'all_may_narratives.json').write_text(json.dumps(list(packets.values()), ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ['status','report_count','with_narrative','flag_counts','intersections']}, indent=2))
    print('Outputs:', args.output.resolve())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', type=Path, default=Path.cwd())
    p.add_argument('--input', type=Path)
    p.add_argument('--run-dir', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--expected-count', type=int, default=343)
    a = p.parse_args()
    a.input = a.input or a.project/'data/processed/reports_clean.csv'
    a.output = a.output or a.project/'outputs/may_screening'
    try:
        run(a)
    except (OSError, ValueError, KeyError, TypeError) as e:
        p.exit(1, f'STOPPED: {e}\n')


if __name__ == '__main__':
    main()
