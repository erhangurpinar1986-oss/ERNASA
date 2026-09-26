import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)


def generate_interview_question(
    cv_text: str,
    position: str,
    previous_answers: str = "",
    question_number: int = 2
) -> str:
    if question_number == 2:
        stage_instruction = """
    Önce önceki mülakat cevabını dikkatlice incele.

    Eğer aday 1. sorudaki cevabında iş geçmişinden, daha önce çalıştığı işlerden,
    görevlerinden veya mesleki deneyimlerinden yeterince bahsetmediyse:
    - Bu soruda adayın iş geçmişini ve deneyimini doğal bir sohbet diliyle öğren.

    Eğer aday 1. sorudaki cevabında iş geçmişi ve deneyimlerinden zaten bahsettiyse:
    - Aynı bilgileri tekrar sorma.
    - Psikolojik dayanıklılık, iletişim, takım çalışması veya çatışma yaklaşımından
      yalnızca birini anlamaya yönelik doğal bir soru sor.

    Tek soruda tek konu sor.
    """

    elif 3 <= question_number <= 8:
        stage_instruction = """
    Bu soru adayın iş ortamındaki davranışını anlamaya yönelik olsun.
    Psikolojik dayanıklılık, iletişim, takım çalışması veya çatışma yaklaşımından yalnızca birini ölç.
    Daha önce sorulmuş konuya geri dönme.
    Klinik veya psikiyatrik değerlendirme yapma.
    """

    else:
        stage_instruction = """
    Bu soru başvurulan pozisyona uygun mesleki bilgi veya pratik iş yapış biçimini ölçsün.
    Sahayla ilişkili pozisyonlarda sade, günlük ve uygulanabilir bir mesleki soru sor.
    Daha önce sorulan mesleki konuyu tekrar etme.
    """

    prompt = f"""
Sen ERNASA adlı yapay zekâ destekli insan kaynakları sisteminin
profesyonel mülakat asistanısın.

BAŞVURULAN POZİSYON:
{position}

ADAYIN CV METNİ:
{cv_text}

ÖNCEKİ MÜLAKAT CEVAPLARI:
{previous_answers}
SORU NUMARASI:
{question_number}

BU SORUNUN AŞAMASI:
{stage_instruction}

KURALLAR:
- Türkçe yaz.
- Sadece TEK bir mülakat sorusu üret.
- Mülakat bir sınav değil, doğal bir iş görüşmesi gibi ilerlemelidir.
- Öncelikle başvurulan pozisyonun seviyesini belirle.
- Daha önce sorulmuş bir soruyu aynı veya benzer anlamla tekrar sorma.
- Önceki soruların konusunu takip et; aynı temayı farklı kelimelerle yeniden sormak yerine yeni bir yetkinlik veya deneyim alanına geç.

POZİSYON SEVİYESİ KURALI:
- İşçi, depo personeli, lojistik personeli, sevkiyat personeli, üretim personeli, operatör, şoför, kurye, mağaza personeli, satış danışmanı ve benzeri saha/operasyon pozisyonlarını "SAHA PERSONELİ" kabul et.
- SAHA PERSONELİ için yönetici veya uzman seviyesinde soru KESİNLİKLE sorma.
- SAHA PERSONELİ sorularında KPI, OTIF, optimizasyon, kapasite yönetimi, strateji, metodoloji, performans metriği, süreç iyileştirme ve benzeri kurumsal/teknik yönetim dili KULLANMA.
- Adaydan yüzdelik sonuç, sayısal başarı veya ölçülebilir çıktı isteme.
- Ancak pozisyon gerçekten yönetici, müdür, uzman veya ekip lideri seviyesindeyse daha analitik sorular sorabilirsin.

SORU TARZI:
- Günlük konuşma Türkçesi kullan.
- Kısa ve kolay anlaşılır sor.
- Tek soruda yalnızca TEK konu sor.
- Adayla karşılıklı konuşuyormuş gibi davran.
- İlk sorular kolay ve tanımaya yönelik olsun.
- Daha sonra adayın verdiği cevaplara göre doğal biçimde derinleş.
- CV'de yazan bilgiyi aynen tekrar sordurma.
- Adayın gerçek çalışma davranışını anlamaya çalış.
- Yargılayıcı veya sorguya çeker gibi konuşma.
- Açıklama, değerlendirme, puanlama veya analiz yazma.
- Çıktıda yalnızca sorunun kendisi olsun.

SAHA PERSONELİ İÇİN İSTENEN SORU TARZINA ÖRNEKLER:
"Yoğun bir iş gününde işlerini nasıl sıraya koyarsın?"
"İş sırasında beklemediğin bir sorun çıktığında genelde ne yaparsın?"
"Takım arkadaşlarınla birlikte çalışırken senin için en önemli şey nedir?"
"Yeni bir işi öğrenmen gerektiğinde nasıl öğrenmeyi tercih edersin?"

Bu örnekleri aynen tekrarlamak zorunda değilsin. Aynı sadelikte ve doğallıkta yeni sorular üret.
- Çıktıda yalnızca sorunun kendisi olsun.
"""

    response = client.responses.create(
        model="gpt-5",
        input=prompt
    )

    question = response.output_text.strip()

    if not question:
        return "Mesleki deneyiminizden, bu pozisyonda size katkı sağlayacağını düşündüğünüz bir örnek paylaşır mısınız?"

    return question


def generate_job_fit_analysis(
    cv_text: str,
    position: str,
    interview_answers: str
) -> str:

    prompt = f"""
Sen ERNASA adlı yapay zeka destekli insan kaynakları sisteminin
işe uygunluk analiz asistanısın.

Adayı yalnızca aşağıdaki bilgiler üzerinden değerlendir.

BAŞVURULAN POZİSYON:
{position}

ADAYIN CV METNİ:
{cv_text}

MÜLAKAT SORU VE CEVAPLARI:
{interview_answers}

Görevin aday hakkında işe alım kararı vermek değil,
İK uzmanına objektif ve açıklanabilir karar desteği sağlamaktır.

Adayı başvurduğu pozisyonun gerçek gerekliliklerine göre değerlendir.

Aşağıdaki başlıklarda 0-100 arasında uyum yüzdesi üret:

- Genel Pozisyon Uyumu
- Mesleki Deneyim Uyumu
- Mesleki Bilgi / Uygulama
- Problem Çözme
- İletişim
- Takım Çalışması

Ardından kısa şekilde:

GÜÇLÜ YÖNLER:
Adayın CV ve mülakat cevaplarıyla desteklenen güçlü yönlerini yaz.

GELİŞTİRİLMESİ GEREKEN ALANLAR:
Pozisyon açısından geliştirilmesi gereken noktaları yaz.

DİKKAT EDİLMESİ GEREKEN NOKTALAR:
Eksik, belirsiz veya doğrulanması gereken bilgiler varsa belirt.
Kanıt yoksa varsayım yapma.

İK DEĞERLENDİRME ÖZETİ:
Adayın pozisyonla hangi yönlerden örtüştüğünü ve hangi alanların
İK tarafından ayrıca değerlendirilmesi gerektiğini kısa ve profesyonel şekilde açıkla.

KURALLAR:
- Türkçe yaz.
- İşe alınmalı veya işe alınmamalı şeklinde karar verme.
- Yaş, cinsiyet, medeni durum ve benzeri kişisel özellikleri değerlendirmeye katma.
- Yalnızca işle ilgili bilgi ve cevapları kullan.
- CV'de veya cevaplarda bulunmayan bilgileri uydurma.
- Her yüzdeyi mevcut kanıtlara göre ver.
- Kısa, anlaşılır ve profesyonel yaz.
"""

    response = client.responses.create(
        model="gpt-5",
        input=prompt
    )

    return response.output_text.strip()