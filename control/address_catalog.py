"""Offline address reference backed by the verified source snapshot."""
import hashlib
import json
import unicodedata
from functools import lru_cache
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent / 'address-sources-2_tty_j1'
KEYS = ('country', 'city', 'district')


def folded(value):
    value = value.strip().casefold().replace('ı', 'i')
    return ''.join(c for c in unicodedata.normalize('NFKD', value) if not unicodedata.combining(c))


def match(value, choices):
    if value in choices:
        return value
    matches = [name for name in choices if folded(name) == folded(value)]
    return matches[0] if len(matches) == 1 else None


@lru_cache(maxsize=1)
def catalog():
    manifest = json.loads((SNAPSHOT / 'manifest.json').read_bytes())

    def read(name, key):
        raw = (SNAPSHOT / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest['files'][name]['sha256']:
            raise ValueError('Adres kaynağı bütünlük kontrolünden geçmedi.')
        data = json.loads(raw)
        return data if isinstance(data, list) else data[key]

    countries = {row['alpha_2']:row['name'] for row in read('iso3166-1.json','3166-1')}
    countries['TR'] = 'Türkiye'
    regions = {code:{} for code in countries}
    subdivisions = read('iso3166-2.json','3166-2')
    by_code = {row['code']:row for row in subdivisions}
    for row in subdivisions:
        if row['code'][:2] != 'TR' and not row.get('parent'):
            regions[row['code'][:2]].setdefault(row['name'],[])
    for row in subdivisions:
        code = row['code'][:2]
        parent = row.get('parent')
        if code == 'TR' or not parent:
            continue
        parent_row = by_code.get(parent) or by_code.get(f'{code}-{parent}')
        if parent_row and not parent_row.get('parent'):
            regions[code][parent_row['name']].append(row['name'])
    provinces = {row['id']:row['name'] for row in read('provinces.json','data')}
    regions['TR'] = {name:[] for name in provinces.values()}
    for row in read('districts.json','data'):
        regions['TR'][provinces[row['provinceId']]].append(row['name'])
    return countries, regions


def country_code(value):
    countries, _ = catalog()
    if value.upper() in countries:
        return value.upper()
    if folded(value) in ('turkiye','turkey'):
        return 'TR'
    name = match(value,countries.values())
    return next((code for code,label in countries.items() if label == name),None)


def validate(details, previous, manual):
    # Keep historical free-text addresses editable without silently normalizing them.
    if previous and all(details[key] == previous[key] for key in KEYS):
        return
    countries, regions = catalog()
    code = country_code(details['country'])
    if not code:
        raise ValueError('Listeden geçerli bir ülke seçin.')
    cities = regions[code]
    city = match(details['city'],cities)
    district = match(details['district'],cities.get(city,()))
    if code == 'TR' and (not city or not district):
        raise ValueError('Türkiye için geçerli bir il ve o ile bağlı ilçe seçin.')
    if code != 'TR' and not manual and (not city or not district):
        raise ValueError('Listeden ilgili bölgeyi seçin veya yurt dışı manuel girişi işaretleyin.')
    details.update(country=countries[code],city=city or details['city'],
                   district=district or details['district'])
