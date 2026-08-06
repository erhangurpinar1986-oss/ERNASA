import re
import unicodedata


def normalize_text(value: str) -> str:
    return (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
        .strip()
    )


def clean_ocr_line(line: str) -> str:
    cleaned = " ".join(line.split())

    for symbol in ("●", "•", "◉", "○", "©", "▪", "▫"):
        cleaned = cleaned.replace(symbol, "")

    return cleaned.strip()


def split_joined_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()

    # OCR'nin "GökhanÇETİN" gibi birleştirdiği ad-soyadı ayırır.
    value = re.sub(
        r"(?<=[a-zçğıöşü])(?=[A-ZÇĞİÖŞÜ]{2,}$)",
        " ",
        value
    )

    return value.title()


def extract_labeled_name(lines: list[str]) -> str:
    labels = {
        "isim",
        "ad soyad",
        "ad-soyad",
        "adi soyadi",
        "ad ve soyad",
        "name",
        "full name"
    }

    for line in lines[:40]:
        clean_line = clean_ocr_line(line)

        if ":" not in clean_line:
            continue

        left, right = clean_line.split(":", 1)
        normalized_left = normalize_text(left)

        if any(label in normalized_left for label in labels):
            candidate_name = split_joined_name(right)

            if len(candidate_name) >= 3:
                return candidate_name

    return ""


def extract_fallback_name(lines: list[str]) -> str:
    ignored_headings = {
        "kisisel bilgiler",
        "iletisim",
        "ozgecmis",
        "cv",
        "curriculum vitae",
        "profil",
        "hakkimda",
        "egitim",
        "egitim durumu",
        "deneyim",
        "is deneyimleri",
        "is tecrubeleri",
        "beceriler",
        "yetenekler",
        "referanslar",
        "yabanci dil ve duzeyi",
        "kurs ve sertifikalar"
    }

    for line in lines[:25]:
        clean_line = clean_ocr_line(line)
        normalized_line = normalize_text(clean_line)

        if normalized_line in ignored_headings:
            continue

        if ":" in clean_line:
            continue

        if "@" in clean_line:
            continue

        if any(character.isdigit() for character in clean_line):
            continue

        words = clean_line.split()

        if not 2 <= len(words) <= 4:
            continue

        if all(
            re.fullmatch(r"[A-Za-zÇĞİÖŞÜçğıöşü'-]+", word)
            for word in words
        ):
            return clean_line.title()

    return ""


def parse_candidate(text: str) -> dict:
    print(">>> PARSER ÇALIŞTI <<<")

    email_match = re.search(
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        text,
        re.IGNORECASE
    )

    phone_match = re.search(
        r"(?:\+90|0)?\s*5\d{2}[\s.\-]?\d{3}[\s.\-]?\d{2}[\s.\-]?\d{2}",
        text
    )

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    candidate_name = extract_labeled_name(lines)

    if not candidate_name:
        candidate_name = extract_fallback_name(lines)

    return {
        "name": candidate_name,
        "surname": "",
        "email": email_match.group(0) if email_match else "",
        "phone": phone_match.group(0) if phone_match else "",
        "education": [],
        "experience": [],
        "skills": []
    }