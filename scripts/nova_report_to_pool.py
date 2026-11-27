"""Convert past "Nova Müşteri Araştırması" Markdown reports into NOVA data-pool import JSON.

Usage:  python scripts/nova_report_to_pool.py rapor1.md rapor2.md ... > nova-gecmis-raporlar.json
Reads only the company table (Şirket | Sektör | İl | İlan | İş yükü alıntısı | Resmî site | Kurumsal e-posta | Puan | Durum).
Keeps a field only if the report gave it with a source URL; "kanıt yetersiz" cells are dropped, nothing is guessed.
The output is validated again by CONTROL on import (corporate-only, sources required, duplicates skipped).
"""
import json
import re
import sys

URL = re.compile(r'https?://[^\s)\]|>]+')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
DATE = re.compile(r'(20\d\d-\d\d-\d\d)|(\d{1,2})[./](\d{1,2})[./](20\d\d)')


def iso(text):
    m = DATE.search(text or '')
    if not m:
        return ''
    if m.group(1):
        return m.group(1)
    return f'{m.group(4)}-{int(m.group(3)):02d}-{int(m.group(2)):02d}'


def cells(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def parse(text, report_date):
    rows, header = [], None
    for line in text.splitlines():
        if not line.strip().startswith('|'):
            header = None if not line.strip() else header
            continue
        parts = cells(line)
        if header is None:
            if parts and parts[0].lower().startswith('şirket'):
                header = [p.replace('İ', 'i').replace('I', 'ı').lower() for p in parts]
            continue
        if set(''.join(parts)) <= set('-: '):
            continue
        row = dict(zip(header, parts))
        name = re.sub(r'[*_`]', '', row.get('şirket', '')).strip()
        if not name or 'kanıt yetersiz' in name.lower():
            continue
        out = {'legal_name': name, 'sector': row.get('sektör', ''), 'city': row.get('il', ''), 'verified_on': report_date}
        site = URL.findall(row.get('resmî site', '') or row.get('resmi site', ''))
        if site:
            out['website'] = site[0]
            out['source_url'] = site[0]
        mail_cell = next((v for k, v in row.items() if 'e-posta' in k), '')
        mails, mail_src = EMAIL.findall(mail_cell), URL.findall(mail_cell)
        if mails and mail_src and 'kanıt yetersiz' not in mail_cell.lower():
            out['email'], out['email_source_url'] = mails[0].lower(), mail_src[0]
        job_cell = next((v for k, v in row.items() if k.startswith('ilan')), '')
        job_src = URL.findall(job_cell)
        if job_src:
            out['job_source_url'] = job_src[0]
            out['open_jobs'] = URL.sub('', job_cell).strip(' ,;-()')[:300]
            out['job_seen_on'] = iso(job_cell)
            out.setdefault('source_url', job_src[0])
        quote = next((v for k, v in row.items() if 'yük' in k), '')
        if quote and 'kanıt yetersiz' not in quote.lower():
            out['workload_terms'] = quote.strip('"“” ')[:300]
        if out.get('source_url'):
            rows.append({k: v for k, v in out.items() if v})
    return rows


def main(paths):
    result, seen = [], set()
    for path in paths:
        text = open(path, encoding='utf-8').read()
        date = iso(path) or iso(text[:500])
        for row in parse(text, date):
            key = (row.get('website') or row['legal_name']).lower()
            if key not in seen:
                seen.add(key)
                result.append(row)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    print(f'\n{len(result)} kayıt', file=sys.stderr)


if __name__ == '__main__':
    main(sys.argv[1:])
