"""ERNASA: kişiye özel bağlantılı, sesli eğitim ve anlık geri bildirim modülü.

B2B satış ortağı adayları için bir eğitim aracıdır; iş/temsil yetkisi vermez.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
import re
import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import get_connection

router = APIRouter()
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
MODULES = [
    {
        "id": "temel",
        "title": "ERNASA ve insan odaklı teknoloji",
        "narration": (
            "ERNASA Yazılım ve Teknoloji, iş süreçlerini iyileştiren dijital çözümler geliştirmeyi hedefler. "
            "Öncelikli anlatımımız ERNASA İK üzerinedir. Bu sistem; aday takibi, dijital mülakat ve insan "
            "kaynakları ekiplerine yönelik raporlama akışlarını destekler. Yapay zekâ karar desteği sunar, "
            "nihai işe alım kararı insana aittir. NEXUS ve diğer projeler için ticari hazırlık iddiasında bulunulmaz."
        ),
        "points": [
            "Ürün özellikleriyle geliştirme vizyonunu birbirinden ayır.",
            "İnsan denetimini ve adil aday deneyimini öne çıkar.",
            "Doğrulanmamış doğruluk, tasarruf veya mevzuat uyumu iddiası verme.",
        ],
        "questions": [
            {
                "id": "temel-1",
                "prompt": "ERNASA İK hakkında hangi ifade doğrudur?",
                "options": ["İşe alım kararlarını otomatik verir.", "İK ekibine karar desteği sunar.", "Klinik psikolojik tanı koyar.", "Tüm şirket sistemleriyle hazır entegredir."],
                "correct": 1,
                "explain": "ERNASA İK'nın anlatımı karar desteğidir; kabul veya ret kararının sahibi yetkili insandır.",
            },
            {
                "id": "temel-2",
                "prompt": "Henüz canlıda doğrulanmamış bir entegrasyon sorulursa ne yapılır?",
                "options": ["Kesin hazır denir.", "Rakiplerde var diye var denir.", "Teknik doğrulama ve kapsam teyidi istenir.", "Sözleşmede gizlenir."],
                "correct": 2,
                "explain": "Ürün kabiliyetleri canlı sürüm ve teknik ekiple doğrulanmadan müşteriye taahhüt edilmez.",
            },
            {
                "id": "temel-3",
                "prompt": "Aday değerlendirmesinde hangi yaklaşım uygundur?",
                "options": ["Sadece tek sayısal puana göre eleme", "Şeffaf, gerekçeli insan denetimi", "Adaya klinik etiket koyma", "Önyargılı kısa yol"],
                "correct": 1,
                "explain": "Açıklanabilirlik, insani denetim ve eşit muamele hedeflenir; otomatik nihai karar verilmez.",
            },
        ],
    },
    {
        "id": "akis",
        "title": "ERNASA İK iş akışını anlatma",
        "narration": (
            "Müşterinin işe alım süreci önce anlaşılır. Aday başvuru veya özgeçmiş kaydı alınır; "
            "uygun olduğunda ön değerlendirme ve mülakat daveti yürütülür. Dijital mülakat akışı "
            "on soruluk yapı ve süreli davet içerir. Ardından rapor yetkili insan kaynakları "
            "ekibi tarafından incelenir. Eğitimdeki örnekler gerçek müşteri kaydı değildir. "
            "Davet süreleri ve erişim kuralları ürünün güncel sürümünde ayrıca doğrulanmalıdır."
        ),
        "points": [
            "Akış: aday kaydı → ön değerlendirme → mülakat → rapor → insan kararı.",
            "CV, mülakat ve raporun erişim yetkileri birbirinden farklı olabilir.",
            "Müşteriye yalnızca doğrulanmış ekran ve yetenekleri göster.",
        ],
        "questions": [
            {
                "id": "akis-1",
                "prompt": "Örnek işe alım iş akışında rapordan sonra kim karar verir?",
                "options": ["Yalnızca yapay zekâ", "Yetkili insan kaynakları ekibi", "Satış ortağı", "Başka aday"],
                "correct": 1,
                "explain": "Rapor kararı destekler; değerlendirme ve işe alım kararı müşteri kurumun yetkili insanındadır.",
            },
            {
                "id": "akis-2",
                "prompt": "Dijital mülakatın eğitimde anlatılan temel yapısı hangisidir?",
                "options": ["Sınırsız sorulu açık sohbet", "Her zaman video zorunluluğu", "On soruluk, süreli davetli akış", "Her yanıt için satış primi"],
                "correct": 2,
                "explain": "Tanıtımda on soruluk ve süreli davet yaklaşımı yer alır; süre güncel yapılandırmadan teyit edilir.",
            },
            {
                "id": "akis-3",
                "prompt": "Müşteri bir ekran örneği istediğinde en doğru yaklaşım hangisidir?",
                "options": ["Temsili ekranı canlı veri gibi sunmak", "Yetkili, doğrulanmış demo göstermek", "Aday verilerini izinsiz göndermek", "Rapor sonucunu garanti etmek"],
                "correct": 1,
                "explain": "Gizlilik korunarak yetkili örnekler gösterilir; temsili ekranların niteliği açıkça belirtilir.",
            },
        ],
    },
    {
        "id": "kesif",
        "title": "Müşteri keşfi ve değer önerisi",
        "narration": (
            "İyi kurumsal satış, ürünle değil müşterinin problemiyle başlar. Önce sektör, işe alım "
            "hacmi, aday yönetim araçları, mülakat darboğazları ve karar verici rolü öğrenilir. "
            "Sonra ölçülebilir hedefler birlikte belirlenir. ERNASA'nın sağlayabileceği değer "
            "kanıtlı ürün kapsamıyla eşleştirilir. Süre veya maliyet tasarrufu garanti edilmez."
        ),
        "points": [
            "Önce sor: Kaç pozisyon, kaç başvuru, hangi süreç sorunu?",
            "Karar vericiyi, zaman planını ve başarı ölçütünü doğrula.",
            "Değeri, müşterinin gerçek iş akışına bağla; ölçülmemiş sonucu vaat etme.",
        ],
        "questions": [
            {
                "id": "kesif-1",
                "prompt": "İlk müşteri görüşmesinde en iyi açılış sorusu hangisidir?",
                "options": ["Bugün sözleşme imzalar mısınız?", "En pahalı paketi alır mısınız?", "Aday ve mülakat sürecinizde nerede zorlanıyorsunuz?", "Rakipler kötüdür, değil mi?"],
                "correct": 2,
                "explain": "İhtiyaç keşfi, çözüm önermeden önce müşterinin mevcut süreç ve darboğazını anlamaktır.",
            },
            {
                "id": "kesif-2",
                "prompt": "Hangi satış vaadi uygun değildir?",
                "options": ["Demo ile güncel kapsamı gösterelim.", "İhtiyaçlarınızı birlikte netleştirelim.", "İşe alım sürenizi kesin yüzde 80 azaltır.", "Önce mevcut iş akışınızı değerlendirelim."],
                "correct": 2,
                "explain": "Gerçek müşteride ölçülmemiş performans veya tasarruf oranı garanti edilemez.",
            },
            {
                "id": "kesif-3",
                "prompt": "Bir fırsatı nitelendirirken hangisi önemlidir?",
                "options": ["Yalnızca şirket logosu", "İhtiyaç, yetkili, zamanlama ve uygunluk", "Ofis binasının rengi", "Sosyal medya takipçi sayısı"],
                "correct": 1,
                "explain": "Nitelendirme ihtiyacın şiddetini, karar sürecini ve gerçek satın alma koşullarını kapsar.",
            },
        ],
    },
    {
        "id": "pazar",
        "title": "Pazar fizibilitesi ve kaynaklı araştırma",
        "narration": (
            "Ön fizibilite, bir ülke ve sektör seçerek potansiyel on kurumu kamuya açık kaynaklarla "
            "incelemeyi amaçlar. Resmî şirket sayfaları, açık iş ilanları ve güvenilir yayınlar "
            "kullanılır. Her firma için kaynak, erişim tarihi, işe alım sinyali ve kısa ihtiyaç "
            "gerekçesi kaydedilir. Bu aşama masa başı araştırmadır; müşteriyle iletişim kurma "
            "veya ERNASA adına teklif verme izni değildir."
        ),
        "points": [
            "10 firma için sektör, ülke, açık işe alım işaretleri ve kaynak tarihi kaydet.",
            "Rakip karşılaştırmasında doğrulanamayan bilgiye 'belirsiz' yaz.",
            "Araştırma yetkisi satış veya temsil yetkisi anlamına gelmez.",
        ],
        "questions": [
            {
                "id": "pazar-1",
                "prompt": "Ön fizibilite aşamasındaki uygun çalışma hangisidir?",
                "options": ["Yetkisiz teklif göndermek", "Kaynak göstererek on firma araştırmak", "Rakip fiyatlarını uydurmak", "Müşteri verisi toplamak"],
                "correct": 1,
                "explain": "Eğitim çalışması doğrulanabilir kaynaklara dayalı on şirketlik masa başı araştırmasıdır.",
            },
            {
                "id": "pazar-2",
                "prompt": "Rakip ürününün fiyatı doğrulanamıyorsa nasıl yazılır?",
                "options": ["Tahmin kesin fiyat olarak verilir.", "Belirsiz / veri bulunamadı.", "En pahalı varsayılır.", "Kontrol tarihini silinir."],
                "correct": 1,
                "explain": "Her rakip verisinde güvenilir kaynak ve kontrol tarihi olmalı; eksik veri belirsiz işaretlenmelidir.",
            },
            {
                "id": "pazar-3",
                "prompt": "Masa başı araştırma yetkisi neyi kapsamaz?",
                "options": ["Resmî siteden bilgi okumayı", "Sektör seçmeyi", "ERNASA adına bağlayıcı teklif vermeyi", "Açık pozisyonları incelemeyi"],
                "correct": 2,
                "explain": "İletişim, fiyat ve teklif vermek için ayrıca ve açıkça yetki gerekir.",
            },
        ],
    },
    {
        "id": "etik",
        "title": "Gizlilik, itirazlar ve yetki sınırları",
        "narration": (
            "Kurumsal güven, doğrulanmış bilgi ve açık yetki sınırlarıyla kurulur. Aday "
            "özgeçmişleri, mülakat yanıtları ve işveren bilgileri yalnızca yetkili kişilerce "
            "erişilmesi gereken kişisel veya gizli bilgiler içerebilir. KVKK ve gerektiğinde "
            "ilgili diğer veri koruma yükümlülükleri hukuki inceleme gerektirir; genel "
            "uyumluluk garantisi verilmez. İtirazları sakince dinle, yanıtlayamadığın "
            "konuyu yetkili ekibe taşı. Eğitim tamamlanması satış sözleşmesi veya temsil yetkisi oluşturmaz."
        ),
        "points": [
            "Özel veri paylaşmadan, kontrollü demo ile anlat.",
            "Fiyat/komisyon/garanti taahhüdünü yetkili onaya bağla.",
            "Eğitim sertifika veya ticari temsil yetkisi değildir.",
        ],
        "questions": [
            {
                "id": "etik-1",
                "prompt": "Müşteri aday CV'lerinden örnek istediğinde ne yapılır?",
                "options": ["İzinsiz gerçek CV paylaşılır.", "Anonim veya sentetik, yetkili örnek sunulur.", "Raporlar herkese açılır.", "Sosyal medyada yayınlanır."],
                "correct": 1,
                "explain": "Gerçek kişisel verileri izinsiz paylaşmak yerine uygun, yetkilendirilmiş örnek kullanılmalıdır.",
            },
            {
                "id": "etik-2",
                "prompt": "Müşteri mevzuata tam uyum garantisi isterse en doğru yanıt hangisidir?",
                "options": ["Hiç incelemeden garanti edilir.", "Konu ürün ve hukuk ekiplerinin doğrulamasına yönlendirilir.", "Soru geçiştirilir.", "Garanti satış ortağınca imzalanır."],
                "correct": 1,
                "explain": "Mevzuat kapsamı, işleme rolleri ve teknik tedbirler somut kullanım senaryosuna göre doğrulanmalıdır.",
            },
            {
                "id": "etik-3",
                "prompt": "Bu eğitimi tamamlamak aşağıdakilerden hangisini tek başına sağlar?",
                "options": ["ERNASA adına imza yetkisi", "Kesin satış ortaklığı statüsü", "Konuya ilişkin eğitim ilerlemesi ve sonuç kaydı", "Sınırsız müşteri verisi erişimi"],
                "correct": 2,
                "explain": "Eğitim tamamlanması yalnızca öğrenme ilerlemesini gösterir; iş ve temsil yetkileri ayrıca düzenlenir.",
            },
        ],
    },
]
QUESTION_BY_ID = {q["id"]: q for m in MODULES for q in m["questions"]}
QUESTION_ORDER = [q["id"] for m in MODULES for q in m["questions"]]


def initialize_schema():
    with closing(get_connection()) as db:
        c = db.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS ernasa_training_invites (
            token_hash TEXT PRIMARY KEY, recipient TEXT NOT NULL,
            email TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS ernasa_training_answers (
            token_hash TEXT NOT NULL, question_id TEXT NOT NULL,
            selected_index INTEGER NOT NULL, answered_at TEXT NOT NULL,
            PRIMARY KEY (token_hash, question_id),
            FOREIGN KEY (token_hash) REFERENCES ernasa_training_invites(token_hash)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS ernasa_training_answers_owner ON ernasa_training_answers(token_hash)")
        db.commit()


def _now():
    return datetime.now(timezone.utc)


def _invite(db, token):
    if not TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(404, "Eğitim bağlantısı bulunamadı.")
    digest = sha256(token.encode("ascii")).hexdigest()
    cur = db.cursor()
    cur.execute("SELECT * FROM ernasa_training_invites WHERE token_hash=?", (digest,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "Eğitim bağlantısı bulunamadı.")
    if row["revoked"] or datetime.fromisoformat(row["expires_at"]) <= _now():
        raise HTTPException(410, "Eğitim bağlantısının süresi dolmuş veya erişim kapatılmış.")
    return digest, row


def validate_token(token):
    with closing(get_connection()) as db:
        _invite(db, token)


class InviteForm(BaseModel):
    recipient: str = Field(min_length=2, max_length=120)
    email: str = Field(default="", max_length=254)
    days: int = Field(default=14, ge=1, le=30)


class AnswerForm(BaseModel):
    question_id: str = Field(min_length=3, max_length=60)
    selected_index: int = Field(ge=0, le=3)


def create_invite(form: InviteForm, public_base: str):
    email = form.email.strip().lower()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(422, "E-posta adresi geçersiz.")
    token = secrets.token_urlsafe(32)
    digest = sha256(token.encode("ascii")).hexdigest()
    created = _now()
    expires = created + timedelta(days=form.days)
    with closing(get_connection()) as db:
        c = db.cursor()
        c.execute("""INSERT INTO ernasa_training_invites
            (token_hash,recipient,email,created_at,expires_at,revoked)
            VALUES (?,?,?,?,?,0)""",
            (digest, form.recipient.strip(), email, created.isoformat(), expires.isoformat()))
        db.commit()
    return {
        "recipient": form.recipient.strip(), "expires_at": expires.isoformat(),
        "link": public_base.rstrip("/") + "/egitim/" + token,
        "sent": False,
    }


def _history(db, digest):
    c = db.cursor()
    c.execute("SELECT question_id,selected_index,answered_at FROM ernasa_training_answers WHERE token_hash=?", (digest,))
    return {r["question_id"]: dict(r) for r in c.fetchall() if r["question_id"] in QUESTION_BY_ID}


def _feedback(question, selected_index):
    return {
        "correct": selected_index == question["correct"],
        "selected_index": selected_index,
        "correct_index": question["correct"],
        "correct_answer": question["options"][question["correct"]],
        "explanation": question["explain"],
    }


def _state(db, digest, row):
    history = _history(db, digest)
    next_id = next((qid for qid in QUESTION_ORDER if qid not in history), None)
    modules = [
        {
            "id": m["id"], "title": m["title"], "narration": m["narration"],
            "points": m["points"],
            "answered": sum(q["id"] in history for q in m["questions"]),
            "total": len(m["questions"]),
        }
        for m in MODULES
    ]
    next_question = None
    if next_id:
        q = QUESTION_BY_ID[next_id]
        next_module = next(m for m in MODULES if any(item["id"] == next_id for item in m["questions"]))
        next_question = {"id": q["id"], "prompt": q["prompt"],
                         "options": q["options"], "module_id": next_module["id"]}
    correct = sum(
        history[qid]["selected_index"] == QUESTION_BY_ID[qid]["correct"]
        for qid in history
    )
    return {
        "recipient": row["recipient"], "expires_at": row["expires_at"],
        "modules": modules, "next_question": next_question,
        "answered": len(history), "total": len(QUESTION_ORDER),
        "correct": correct,
        "completed": next_id is None,
        "percentage": round(correct * 100 / len(QUESTION_ORDER)),
        "notice": "Eğitim katılımı, işe alım veya ERNASA adına satış yetkisi oluşturmaz.",
    }


@router.get("/api/egitim/{token}/durum")
def get_state(token: str):
    with closing(get_connection()) as db:
        digest, row = _invite(db, token)
        return _state(db, digest, row)


@router.post("/api/egitim/{token}/yanit")
def post_answer(token: str, fields: AnswerForm):
    with closing(get_connection()) as db:
        digest, row = _invite(db, token)
        history = _history(db, digest)
        next_id = next((qid for qid in QUESTION_ORDER if qid not in history), None)
        if not next_id:
            raise HTTPException(409, "Eğitim zaten tamamlanmış.")
        if fields.question_id != next_id:
            raise HTTPException(409, "Lütfen sıradaki soruyu yanıtlayın.")
        question = QUESTION_BY_ID[next_id]
        if fields.selected_index >= len(question["options"]):
            raise HTTPException(422, "Seçenek geçersiz.")
        c = db.cursor()
        c.execute("""INSERT INTO ernasa_training_answers
            (token_hash,question_id,selected_index,answered_at) VALUES(?,?,?,?)
            ON CONFLICT (token_hash,question_id) DO NOTHING""",
            (digest, next_id, fields.selected_index, _now().isoformat()))
        db.commit()
        saved = _history(db, digest)[next_id]
        return {"feedback": _feedback(question, saved["selected_index"]),
                "state": _state(db, digest, row)}
