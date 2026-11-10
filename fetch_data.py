#!/usr/bin/env python3
import argparse
import calendar
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

URL = 'https://api.fda.gov/device/event.json'
PAGE_SIZE = 1000
MAX_WINDOW = 26000  # max skip 25000 plus a final page of 1000


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Downloader:
    def __init__(self, root, product, year):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.session = requests.Session()
        self.api_key = os.environ.get('OPENFDA_API_KEY')
        self.keys = set()
        self.manifest = {
            'status': 'running', 'started_at_utc': utcnow(),
            'endpoint': URL, 'product_code': product, 'receipt_year': year,
            'page_size': PAGE_SIZE, 'requests': [], 'windows': [], 'months': [],
            'notes': 'Live API extract; report-key counts are not incident counts.',
        }
        self.save_manifest()

    def save_manifest(self):
        path = self.root / 'manifest.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.manifest, indent=2), encoding='utf-8')
        temporary.replace(path)

    def query(self, start, end):
        return (f'device.device_report_product_code:"{self.manifest["product_code"]}" '
                f'AND date_received:[{start:%Y%m%d} TO {end:%Y%m%d}]')

    def request(self, query, label, skip=0, limit=1):
        public_params = {'search': query, 'sort': 'date_received:asc',
                         'skip': skip, 'limit': limit}
        params = dict(public_params)
        if self.api_key:
            params['api_key'] = self.api_key
        for attempt in range(6):
            time.sleep(0.3)
            try:
                response = self.session.get(URL, params=params, timeout=(15, 90))
            except requests.RequestException:
                if attempt == 5:
                    raise RuntimeError('Network request failed after 6 attempts') from None
                time.sleep(min(2 ** attempt, 30))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 5:
                    raise RuntimeError(f'HTTP {response.status_code} after 6 attempts')
                retry = response.headers.get('Retry-After', '')
                delay = min(float(retry), 60) if retry.isdigit() else min(2 ** attempt, 30)
                time.sleep(delay)
                continue
            # Preserve the response body without reformatting or modifying JSON.
            name = f'{len(self.manifest["requests"]):05d}_{label}.json'
            body = response.content
            (self.root / name).write_bytes(body)
            entry = {'file': name, 'query': query, 'params': public_params,
                     'received_at_utc': utcnow(), 'http_status': response.status_code,
                     'sha256': hashlib.sha256(body).hexdigest()}
            self.manifest['requests'].append(entry)
            self.save_manifest()
            try:
                payload = response.json()
            except ValueError:
                raise RuntimeError(f'Non-JSON response; inspect {name}') from None
            if (response.status_code == 404 and
                    payload.get('error', {}).get('code') == 'NOT_FOUND' and
                    'No matches found' in payload.get('error', {}).get('message', '')):
                return 0, [], name
            if response.status_code != 200:
                raise RuntimeError(f'HTTP {response.status_code}; inspect {name}')
            total = int(payload['meta']['results']['total'])
            return total, payload['results'], name
        raise RuntimeError('Request retry loop exhausted')

    def window(self, start, end):
        query = self.query(start, end)
        label = f'{start:%Y%m%d}_{end:%Y%m%d}'
        expected, _, _ = self.request(query, label + '_count')
        item = {'start': str(start), 'end': str(end), 'query': query,
                'expected_total': expected, 'status': 'running', 'page_files': []}
        self.manifest['windows'].append(item)
        self.save_manifest()
        if expected > MAX_WINDOW:
            if start == end:
                raise RuntimeError('One day exceeds 26,000 reports; use FDA bulk downloads '
                                   'or search-after. No data have been silently truncated.')
            middle = start + timedelta(days=(end - start).days // 2)
            print(f'Splitting {start} through {end}: {expected:,} reports', flush=True)
            count = self.window(start, middle) + self.window(middle + timedelta(days=1), end)
            if count != expected:
                raise RuntimeError('Split-window total changed during extraction; rerun.')
            item.update(status='split_verified', downloaded_total=count)
            self.save_manifest()
            return count
        local_keys = set()
        downloaded = 0
        for skip in range(0, expected, PAGE_SIZE):
            limit = min(PAGE_SIZE, expected - skip)
            total, records, name = self.request(query, label + f'_page_{skip:06d}', skip, limit)
            item['page_files'].append(name)
            if total != expected or len(records) != limit:
                raise RuntimeError(f'Count/page-length mismatch in {name}; rerun.')
            for record in records:
                key = str(record.get('mdr_report_key', '')).strip()
                if not key or key == 'None':
                    raise RuntimeError(f'Missing report key in {name}')
                if key in local_keys or key in self.keys:
                    raise RuntimeError(f'Repeated report key {key}; extract is not verified.')
                received = str(record.get('date_received', ''))
                if not (re.fullmatch(r'\d{8}', received) and
                        f'{start:%Y%m%d}' <= received <= f'{end:%Y%m%d}'):
                    raise RuntimeError(f'Unexpected receipt date for report {key}')
                local_keys.add(key)
            downloaded += len(records)
            print(f'{start} to {end}: {downloaded:,}/{expected:,}', flush=True)
        after, _, _ = self.request(query, label + '_count_after')
        if after != expected or len(local_keys) != expected:
            raise RuntimeError('Final count reconciliation failed; rerun.')
        self.keys.update(local_keys)
        item.update(status='verified', downloaded_total=downloaded,
                    unique_report_keys=len(local_keys), expected_total_after=after)
        self.save_manifest()
        return downloaded

    def run(self):
        year = self.manifest['receipt_year']
        try:
            for month in range(1, 13):
                start = date(year, month, 1)
                end = date(year, month, calendar.monthrange(year, month)[1])
                total = self.window(start, end)
                self.manifest['months'].append({'month': f'{year}-{month:02d}',
                                               'verified_total': total})
                self.save_manifest()
            expected, _, _ = self.request(self.query(date(year, 1, 1), date(year, 12, 31)),
                                           'year_final_count')
            downloaded = sum(m['verified_total'] for m in self.manifest['months'])
            if not expected == downloaded == len(self.keys):
                raise RuntimeError('Annual expected/downloaded/unique counts differ; rerun.')
            self.manifest.update(status='complete', expected_total=expected,
                                 downloaded_total=downloaded, unique_report_keys=len(self.keys))
            print(f'COMPLETE: {downloaded:,} records; {len(self.keys):,} unique report keys.')
        except BaseException as exc:
            self.manifest.update(status='failed', error=str(exc) or type(exc).__name__)
            raise
        finally:
            self.manifest['finished_at_utc'] = utcnow()
            self.save_manifest()
            self.session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--year', type=int, default=2025)
    parser.add_argument('--product-code', default='LOX')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'data' / 'raw')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Z]{3}', args.product_code) or not 2009 <= args.year <= date.today().year:
        parser.error('Use a three-letter uppercase product code and a valid year from 2009 onward.')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8]
    root = args.output / f'{args.product_code}_{args.year}_{run_id}'
    print(f'Writing this extraction to: {root}', flush=True)
    downloader = Downloader(root, args.product_code, args.year)
    try:
        downloader.run()
    except (Exception, KeyboardInterrupt) as exc:
        print(f'STOPPED: {exc}\nInspect {root / "manifest.json"}; do not analyze this failed run.',
              file=sys.stderr)
        return 1
    print(f'Manifest: {root / "manifest.json"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
