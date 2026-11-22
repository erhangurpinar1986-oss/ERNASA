"""ERNASA HR FLOW: AI mülakat raporunun bölüm yapısı (tek tanım).

AI raporu dört ayrı değerlendirme başlığı ve bir özet içerir:
  GÜÇLÜ YÖNLER, ZAYIF YÖNLER, GELİŞTİRİLMESİ GEREKEN YÖNLER, DİKKAT EDİLMESİ GEREKENLER,
  GENEL İK DEĞERLENDİRME ÖZETİ.

Eski (üç başlıklı) raporlar da okunur. Eski "Geliştirilmesi gereken alanlar" maddeleri
asla "Zayıf yönler"e taşınmaz: kanıtsız sınıflandırma üretilmez.
Bu modül metni yorumlamaz, puan veya karar üretmez.
"""
import re

NO_FINDING = "Bu alanda doğrulanmış bulgu bulunmuyor."
SECTIONS = (
    ("strengths", "GÜÇLÜ YÖNLER", "Güçlü Yönler"),
    ("weaknesses", "ZAYIF YÖNLER", "Zayıf Yönler"),
    ("development", "GELİŞTİRİLMESİ GEREKEN YÖNLER", "Geliştirilmesi Gereken Yönler"),
    ("attention", "DİKKAT EDİLMESİ GEREKENLER", "Dikkat Edilmesi Gerekenler"),
    ("summary", "GENEL İK DEĞERLENDİRME ÖZETİ", "Genel İK Değerlendirme Özeti"),
)
# Eski rapor başlıkları (geriye dönük uyumluluk). Yalnızca aynı anlamdaki alana eşlenir.
ALIASES = {
    "GÜÇLÜ YÖNLER": "strengths",
    "ZAYIF YÖNLER": "weaknesses",
    "GELİŞTİRİLMESİ GEREKEN YÖNLER": "development",
    "GELİŞTİRİLMESİ GEREKEN ALANLAR": "development",
    "DİKKAT EDİLMESİ GEREKENLER": "attention",
    "DİKKAT EDİLMESİ GEREKEN NOKTALAR": "attention",
    "GENEL İK DEĞERLENDİRME ÖZETİ": "summary",
    "İK DEĞERLENDİRME ÖZETİ": "summary",
}
_HEADING = re.compile(r"^\s*(?:#+\s*)?\**\s*([A-ZÇĞİÖŞÜa-zçğıöşü][A-ZÇĞİÖŞÜa-zçğıöşü /]{3,}?)\s*\**\s*:\s*\**\s*$")
_EMPTY = {"bu alanda doğrulanmış bulgu bulunmuyor", "doğrulanmış bulgu bulunmuyor", "yok", "bulunmuyor", "-"}


def _upper(text):
    return text.replace("i", "İ").replace("ı", "I").upper()


def _is_empty_statement(line):
    return line.strip().rstrip(".").strip().lower() in _EMPTY


def parse(text):
    """Bölümleri ayırır. Dönüş: alan → madde listesi, format ve rapordaki eksik başlıklar."""
    result = {key: [] for key, _, _ in SECTIONS}
    seen, current, other = set(), None, False
    for raw in (text or "").splitlines():
        match = _HEADING.match(raw)
        if match:
            key = ALIASES.get(_upper(match.group(1).strip()))
            current = key
            if key:
                seen.add(key)
            else:
                other = True
            continue
        line = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", raw).strip()
        if current and line and not _is_empty_statement(line):
            result[current].append(line)
    fmt = "four_section" if "weaknesses" in seen else ("legacy" if seen else "unstructured")
    return {**result, "format": fmt, "missing": [key for key, _, _ in SECTIONS if key not in seen],
            "has_other_sections": other}


def normalize(text):
    """AI çıktısında eksik kalan başlığı açıkça 'bulgu yok' notuyla ekler; mevcut içeriği değiştirmez."""
    text = (text or "").rstrip()
    present = parse(text)
    additions = [f"{heading}:\n{NO_FINDING}" for key, heading, _ in SECTIONS if key in present["missing"]]
    return text + ("\n\n" + "\n\n".join(additions) if additions else "") + "\n"
