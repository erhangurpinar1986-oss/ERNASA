"""E.164 phone handling and masking (ERNASA SECURITY 2026, section 8)."""
import re

E164 = re.compile(r'^\+[1-9]\d{7,14}$')


class PhoneError(ValueError):
    pass


def normalize(raw, default_country='90'):
    """Return an E.164 number. Turkish local forms (05xx..., 5xx...) get +90."""
    digits = re.sub(r'[\s().-]', '', str(raw or ''))
    if digits.startswith('00'):
        digits = '+' + digits[2:]
    if not digits.startswith('+'):
        if default_country == '90':
            if re.fullmatch(r'0?5\d{9}', digits):
                digits = '+90' + digits[-10:]
            else:
                raise PhoneError('Ülke koduyla birlikte geçerli bir telefon numarası girin.')
        else:
            digits = '+' + default_country + digits.lstrip('0')
    if not E164.fullmatch(digits):
        raise PhoneError('Ülke koduyla birlikte geçerli bir telefon numarası girin.')
    if digits.startswith('+90') and not re.fullmatch(r'\+90[2-5]\d{9}', digits):
        raise PhoneError('Türkiye numarası 10 haneli olmalıdır.')
    return digits


def mask(e164):
    """+905321234567 -> +90 532 *** ** 67 ; never show more than the last two digits."""
    if not e164 or not E164.fullmatch(e164):
        return '***'
    if e164.startswith('+90') and len(e164) == 13:
        return f'+90 {e164[3:6]} *** ** {e164[-2:]}'
    return e164[:3] + '*' * (len(e164) - 5) + e164[-2:]
