"""Additive company profile, contact, address and subscription records."""
import re
from io import BytesIO
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

PRODUCT = 'ernasa-ik'
STAGES = ('Potansiyel müşteri','Demo','Teklif','Sözleşme','Aktif','Askıda','Ayrılan müşteri')
TYPES = ('Şahıs işletmesi','Limited şirket','Anonim şirket','Kollektif şirket','Komandit şirket','Kooperatif','Diğer')
CONTACTS = ('Şirket yetkilisi','İK','Finans','Teknik','Satış Temsilcisi','Ana iletişim')
PAYMENTS = ('Belirtilmedi','Bekliyor','Ödendi','Gecikmiş','İade edildi')
PROFILE = {
    'brand':('İşletme / marka adı',120), 'company_type':('Şirket türü',40),
    'tax_id':('Vergi kimlik numarası (VKN)',10), 'tax_office':('Vergi dairesi',100),
    'mersis':('MERSİS numarası',16), 'registry':('Ticaret sicil numarası',40),
    'founded':('Kuruluş tarihi',10), 'sector':('Ana sektör',100), 'subsector':('Alt sektör',100),
    'nace':('NACE kodu',12), 'nace_description':('NACE açıklaması',200), 'nace_version':('NACE sürümü',40),
    'activity':('Faaliyet açıklaması',450), 'size_range':('Tahmini çalışan sayısı aralığı',40),
    'branches':('Şube sayısı',8), 'phone':('Kurumsal telefon',35), 'email':('Genel e-posta',254),
    'accounting_email':('Muhasebe e-postası',254), 'website':('Web sitesi',254),
}
ADDRESS = {'kind':('Adres türü',20),'address':('Adres',450),'city':('İl',80),'district':('İlçe',80),
           'postal':('Posta kodu',20),'country':('Ülke',80)}
CONTACT = {'name':('Ad soyad',120),'title':('Görev / unvan',100),'department':('Departman',100),
           'phone':('İş telefonu',35),'email':('Kurumsal e-posta',254),'kind':('Yetkili türü',30)}
SERVICE = {'product':('Ürün',40),'version':('Paket sürümü',40),'user_quota':('Kullanıcı kotası',9),
           'operation_quota':('İşlem kotası',9),'trial_days':('Deneme günü',4),'period':('Ödeme dönemi',20),
           'price':('Net fiyat',15),'currency':('Para birimi',3),'vat':('KDV oranı (%)',6),
           'payment':('Ödeme durumu',30)}

DDL = """
CREATE TABLE IF NOT EXISTS company_profile(
 company_id INTEGER PRIMARY KEY REFERENCES companies(id), code TEXT NOT NULL UNIQUE,
 stage TEXT NOT NULL DEFAULT 'Potansiyel müşteri',
 parent_id INTEGER REFERENCES companies(id), activated INTEGER NOT NULL DEFAULT 0,
 """ + ','.join(key+" TEXT NOT NULL DEFAULT ''" for key in PROFILE) + """);
CREATE TRIGGER IF NOT EXISTS immutable_company_id BEFORE UPDATE OF id ON companies WHEN old.id<>new.id
 BEGIN SELECT RAISE(ABORT,'immutable company id'); END;
CREATE TRIGGER IF NOT EXISTS immutable_company_code BEFORE UPDATE OF code ON company_profile WHEN old.code<>new.code
 BEGIN SELECT RAISE(ABORT,'immutable company code'); END;
CREATE TABLE IF NOT EXISTS company_contacts(id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
 primary_contact INTEGER NOT NULL DEFAULT 0 CHECK(primary_contact IN (0,1)),
 """ + ','.join(key+' TEXT NOT NULL' for key in CONTACT) + """);
CREATE UNIQUE INDEX IF NOT EXISTS one_primary_contact ON company_contacts(company_id) WHERE primary_contact=1;
CREATE TABLE IF NOT EXISTS company_addresses(id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
 """ + ','.join(key+' TEXT NOT NULL' for key in ADDRESS) + """);
CREATE UNIQUE INDEX IF NOT EXISTS one_core_address ON company_addresses(company_id,kind) WHERE kind IN ('Merkez','Fatura');
CREATE TABLE IF NOT EXISTS products(code TEXT PRIMARY KEY, name TEXT NOT NULL);
INSERT OR IGNORE INTO products VALUES('ernasa-ik','ERNASA İK');
CREATE TABLE IF NOT EXISTS service_terms(company_id INTEGER PRIMARY KEY REFERENCES subscriptions(company_id),
 product TEXT REFERENCES products(code), version TEXT, user_quota INTEGER, operation_quota INTEGER, trial_days INTEGER,
 period TEXT, price_minor INTEGER, currency TEXT, vat_basis INTEGER, payment TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subscription_history(id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
 snapshot TEXT NOT NULL, recorded TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS company_modules(company_id INTEGER REFERENCES companies(id), product TEXT REFERENCES products(code),
 module_code TEXT NOT NULL, PRIMARY KEY(company_id,product,module_code));
CREATE TABLE IF NOT EXISTS company_events(id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
 kind TEXT NOT NULL, reference TEXT, recorded TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS company_usage(company_id INTEGER REFERENCES companies(id), product TEXT REFERENCES products(code),
 period TEXT, units INTEGER NOT NULL, cost_minor INTEGER, currency TEXT, PRIMARY KEY(company_id,product,period));
CREATE TABLE IF NOT EXISTS company_checklist(company_id INTEGER REFERENCES companies(id), item TEXT,
 completed TEXT, PRIMARY KEY(company_id,item));
CREATE TABLE IF NOT EXISTS company_logos(company_id INTEGER PRIMARY KEY REFERENCES companies(id), png BLOB NOT NULL);
INSERT OR IGNORE INTO company_profile(company_id,code,stage)
 SELECT id,printf('ERN-%08d',id),CASE WHEN status='Aktif' THEN 'Aktif' ELSE 'Askıda' END FROM companies;
CREATE TRIGGER IF NOT EXISTS company_profile_insert AFTER INSERT ON companies
 BEGIN INSERT INTO company_profile(company_id,code) VALUES(new.id,printf('ERN-%08d',new.id)); END;
"""


class Invalid(ValueError):
    pass


def logo_png(raw):
    if not isinstance(raw,bytes) or not 1 <= len(raw) <= 512000:
        raise Invalid('Logo dosyası en fazla 500 KB olmalıdır.')
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        raise Invalid('Logo işlemek için Pillow gerekir.') from None
    try:
        with Image.open(BytesIO(raw),formats=('PNG','JPEG')) as image:
            if max(image.size)>1024 or getattr(image,'n_frames',1)!=1:
                raise Invalid('Logo en fazla 1024×1024 piksellik tek kare olmalıdır.')
            output = BytesIO()
            # Rebuild pixels so metadata, profiles and trailing bytes are discarded.
            clean = Image.new('RGBA',image.size)
            clean.paste(image.convert('RGBA'))
            clean.save(output,format='PNG')
            if output.tell()>1_500_000:
                raise Invalid('İşlenen logo boyut sınırını aşıyor.')
            return output.getvalue()
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError):
        raise Invalid('Geçerli bir PNG veya JPEG yükleyin.') from None


def values(data, fields):
    result = {key:data.get(key,'').strip() for key in fields}
    for key, val in result.items():
        label, maximum = fields[key]
        if len(val)>maximum or any(ord(ch)<32 for ch in val):
            raise Invalid(f'{label} geçersiz veya çok uzun.')
    for key in ('email','accounting_email'):
        if result.get(key) and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',result[key]):
            raise Invalid('E-posta biçimini kontrol edin.')
    if result.get('phone') and not re.fullmatch(r'[+0-9 ()-]{5,35}',result['phone']):
        raise Invalid('Telefon biçimini kontrol edin.')
    return result


def profile_values(data):
    result = values(data,PROFILE)
    for key, length in (('tax_id',10),('mersis',16)):
        if result[key] and not re.fullmatch(f'[0-9]{{{length}}}',result[key]):
            raise Invalid(f'{PROFILE[key][0]} {length} rakam olmalıdır.')
    if result['company_type'] and result['company_type'] not in TYPES:
        raise Invalid('Şirket türü geçersiz.')
    if result['nace'] and not re.fullmatch(r'[0-9]{2}(?:\.[0-9]{1,2}){1,2}',result['nace']):
        raise Invalid('NACE kod biçimi geçersiz.')
    if result['nace'] and not result['nace_version']:
        raise Invalid('NACE kodu için sınıflama sürümü gerekir.')
    if result['founded']:
        try:
            founded = date.fromisoformat(result['founded'])
        except ValueError:
            raise Invalid('Kuruluş tarihi geçersiz.') from None
        if founded>date.today():
            raise Invalid('Kuruluş tarihi gelecekte olamaz.')
    if result['branches'] and not re.fullmatch(r'[0-9]+',result['branches']):
        raise Invalid('Şube sayısı sıfır veya pozitif tam sayı olmalıdır.')
    if result['website']:
        try:
            url = urlsplit(result['website'])
            valid = url.scheme in ('http','https') and bool(url.hostname) and not url.username and not url.password
        except ValueError:
            valid = False
        if not valid or any(c.isspace() for c in result['website']):
            raise Invalid('Web sitesi tam bir HTTP veya HTTPS adresi olmalıdır.')
    return result


def activation(db, tenant):
    profile = db.execute('SELECT * FROM company_profile WHERE company_id=?',(tenant,)).fetchone()
    if not all(profile[key] for key in ('company_type','tax_id','tax_office')):
        raise Invalid('Aktivasyon için şirket türü, VKN ve vergi dairesi gerekir.')
    if profile['company_type'] not in ('Şahıs işletmesi','Diğer') and not profile['registry']:
        raise Invalid('Bu şirket türü için ticaret sicil numarası gerekir.')
    if not db.execute("SELECT 1 FROM company_addresses WHERE company_id=? AND kind='Fatura'",(tenant,)).fetchone():
        raise Invalid('Aktivasyon için fatura adresi gerekir.')


def scaled(value, label):
    if not value:
        return None
    if not re.fullmatch(r'[0-9]+(?:[.,][0-9]{1,2})?',value):
        raise Invalid(label+' iki ondalık basamaklı pozitif sayı olmalıdır.')
    return int(Decimal(value.replace(',','.'))*100)


def terms_values(db,data):
    v = values(data,SERVICE)
    if not db.execute('SELECT 1 FROM products WHERE code=?',(v['product'],)).fetchone():
        raise Invalid('Katalog ürünü geçersiz.')
    if v['period'] not in ('','Aylık','Yıllık') or v['payment'] not in PAYMENTS or v['currency'] not in ('','TRY','EUR','USD'):
        raise Invalid('Dönem, para birimi veya ödeme durumu geçersiz.')
    nums = []
    for key in ('user_quota','operation_quota','trial_days'):
        if v[key] and not re.fullmatch(r'[0-9]+',v[key]):
            raise Invalid(SERVICE[key][0]+' tam sayı olmalıdır.')
        nums.append(int(v[key]) if v[key] else None)
    price,vat = scaled(v['price'],'Fiyat'),scaled(v['vat'],'KDV')
    if (price is not None and not v['currency']) or (vat is not None and vat>10000):
        raise Invalid('Fiyat için para birimi gerekir ve KDV %0–100 olmalıdır.')
    return (v['product'],v['version'],*nums,v['period'],price,v['currency'],vat,v['payment'])
