const modalOverlay = document.getElementById("modalOverlay");
const modalTitle = document.getElementById("modalTitle");
const modalContent = document.getElementById("modalContent");

const openKvkkButton = document.getElementById("openKvkkButton");
const openConsentButton = document.getElementById("openConsentButton");

const closeModalButton = document.getElementById("closeModalButton");
const modalCloseAction = document.getElementById("modalCloseAction");

const kvkkCheckbox = document.getElementById("kvkkCheckbox");
const consentCheckbox = document.getElementById("consentCheckbox");
const accuracyCheckbox = document.getElementById("accuracyCheckbox");
const interviewQuestionArea = document.getElementById("interviewQuestionArea");
const questionProgress = document.getElementById("questionProgress");
const questionText = document.getElementById("questionText");
const answerText = document.getElementById("answerText");
const submitAnswerButton = document.getElementById("submitAnswerButton");
const consentCard = document.getElementById("consentCard");
const startInterviewButton = document.getElementById(
    "startInterviewButton"
);
const pathParts = window.location.pathname.split("/");
const interviewToken = pathParts[pathParts.length - 1];
const kvkkText = `
    <h3>
        6698 Sayılı Kişisel Verilerin Korunması Kanunu
        Kapsamında Aydınlatma Metni
    </h3>

    <p>
        Başvurunuz ve dijital ön mülakat süreci kapsamında
        paylaştığınız kişisel veriler, işe alım sürecinin
        yürütülmesi ve başvurunuzun değerlendirilmesi
        amacıyla işlenmektedir.
    </p>

    <p>
        İşlenen veriler; kimlik, iletişim, öz geçmiş,
        mesleki deneyim, eğitim ve mülakat sırasında
        verdiğiniz cevaplardan oluşabilir.
    </p>

    <p>
        Kişisel verileriniz, kanuni yükümlülükler ve
        işe alım sürecinin yürütülmesi için gerekli olan
        durumlar dışında yetkisiz üçüncü kişilerle
        paylaşılmayacaktır.
    </p>

    <p>
        Verileriniz gerekli teknik ve idari güvenlik
        tedbirleri alınarak saklanacaktır.
    </p>

    <p>
        6698 sayılı Kanun'un 11. maddesi kapsamında;
        kişisel verilerinizin işlenip işlenmediğini öğrenme,
        işlenmişse bilgi talep etme, düzeltilmesini veya
        silinmesini isteme haklarına sahipsiniz.
    </p>
`;

const consentText = `
    <h3>
        6698 Sayılı KVKK Kapsamında Açık Rıza Metni
    </h3>

    <p>
        Dijital ön mülakat kapsamında paylaştığım
        kişisel verilerin, işe alım başvurumun
        değerlendirilmesi amacıyla işlenmesini
        kabul ediyorum.
    </p>

    <p>
        Verdiğim rızanın özgür irademe dayandığını,
        açık rızamı dilediğim zaman geri çekebileceğimi
        bildiğimi beyan ederim.
    </p>

    <p>
        Kişisel verilerimin yasal zorunluluklar dışında
        yetkisiz üçüncü kişilerle paylaşılmayacağı
        konusunda bilgilendirildim.
    </p>
`;

function openModal(title, content) {
    modalTitle.textContent = title;
    modalContent.innerHTML = content;
    modalOverlay.hidden = false;
    document.body.style.overflow = "hidden";
}

function closeModal() {
    modalOverlay.hidden = true;
    document.body.style.overflow = "";
}

function updateStartButton() {
    const allApproved =
        kvkkCheckbox.checked &&
        consentCheckbox.checked &&
        accuracyCheckbox.checked;

    startInterviewButton.disabled = !allApproved;
}

openKvkkButton.addEventListener("click", () => {
    openModal("KVKK Aydınlatma Metni", kvkkText);
});

openConsentButton.addEventListener("click", () => {
    openModal("Açık Rıza Metni", consentText);
});

closeModalButton.addEventListener("click", closeModal);
modalCloseAction.addEventListener("click", closeModal);

modalOverlay.addEventListener("click", event => {
    if (event.target === modalOverlay) {
        closeModal();
    }
});

document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !modalOverlay.hidden) {
        closeModal();
    }
});

kvkkCheckbox.addEventListener("change", updateStartButton);
consentCheckbox.addEventListener("change", updateStartButton);
accuracyCheckbox.addEventListener("change", updateStartButton);

startInterviewButton.addEventListener("click", () => {
    if (startInterviewButton.disabled) {
        return;
    }

    fetch(`https://ernasa.com/api/interviews/${interviewToken}/start`, {
        method: "POST"
    })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            console.log("Mülakat başladı:", data);

if (data.success) {
    interviewQuestionArea.style.display = "block";

    questionProgress.textContent = `Soru ${data.question_number}`;
    questionText.textContent = data.question;

    consentCard.style.display = "none";
    startInterviewButton.style.display = "none";
}
        })
        .catch(error => {
            console.error("Mülakat başlatılamadı:", error);
            alert("Mülakat başlatılırken bir hata oluştu.");
        });
});

updateStartButton();
if (interviewToken) {
    fetch(`https://ernasa.com/api/interview-links/${interviewToken}`)
        .then(response => response.json())
        .then(data => {
            console.log("Aday Bilgileri:", data);

            if (data.success) {
                const candidateGreeting = document.getElementById("candidateGreeting");
                const companyName = document.getElementById("companyName");

                if (candidateGreeting) {
                    candidateGreeting.textContent = data.candidate.name;
                }

                if (companyName) {
                    companyName.textContent = data.candidate.company;
                }
            }
        })
        .catch(error => {
            console.error("Mülakat bilgisi alınamadı:", error);
        });
}
submitAnswerButton.addEventListener("click", () => {
    const answer = answerText.value.trim();

    if (!answer) {
        alert("Lütfen cevabınızı yazınız.");
        return;
    }

    const currentQuestionNumber = Number(
        questionProgress.textContent.replace("Soru", "").trim()
    );

    submitAnswerButton.disabled = true;
    submitAnswerButton.textContent = "GÖNDERİLİYOR...";

    fetch(`https://ernasa.com/api/interviews/${interviewToken}/answer`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            answer: answer,
            question_number: currentQuestionNumber
        })
    })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            return response.json();
        })
        .then(data => {
            if (data.success) {
                questionProgress.textContent = `Soru ${data.question_number}`;
                questionText.textContent = data.question;
                answerText.value = "";
                answerText.focus();
            }
        })
        .catch(error => {
            console.error("Cevap gönderilemedi:", error);
            alert("Cevap gönderilirken bir hata oluştu.");
        })
        .finally(() => {
            submitAnswerButton.disabled = false;
            submitAnswerButton.textContent = "CEVABI GÖNDER";
        });
});