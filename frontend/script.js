const modalOverlay = document.getElementById("modalOverlay");
const modalTitle = document.getElementById("modalTitle");
const modalContent = document.getElementById("modalContent");

const openKvkkButton = document.getElementById("openKvkkButton");
const openConsentButton = document.getElementById("openConsentButton");

const closeModalButton = document.getElementById("closeModalButton");
const modalCloseAction = document.getElementById("modalCloseAction");
const welcomeCard = document.querySelector(".welcome-card");
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
if (interviewToken) {
    fetch(`/api/interviews/${interviewToken}/opened`, {
        method: "POST"
    }).catch(error => {
        console.error("Mülakat açılma durumu güncellenemedi:", error);
    });
}

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

let interviewStarting = false;
startInterviewButton.addEventListener("click", () => {
    if (startInterviewButton.disabled || interviewStarting) {
        return;
    }

    interviewStarting = true;
    startInterviewButton.disabled = true;

    fetch(`/api/interviews/${interviewToken}/start`, {
        method: "POST"
    })
        .then(async response => {
            if (!response.ok) {
                let detail = "";
                try {
                    const body = await response.json();
                    detail = typeof body.detail === "string" ? body.detail : "";
                } catch (_) {
                    // Sunucu JSON dışında yanıt vermiş olabilir.
                }
                const error = new Error(detail || `HTTP ${response.status}`);
                error.status = response.status;
                throw error;
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
    welcomeCard.style.display = "none";
    startInterviewButton.style.display = "none";
}
        })
        .catch(error => {
            console.error("Mülakat başlatılamadı:", error);
            if (error.status === 410) {
                alert(
                    "MÜLAKAT BAĞLANTINIZIN SÜRESİ DOLDU\n\n" +
                    "Mülakata başlamak için tanınan 8 saatlik süre sona ermiştir. " +
                    "Bu bağlantıyla yeni bir mülakat başlatamazsınız.\n\n" +
                    "Yeni bağlantı için başvurduğunuz kurumun İnsan Kaynakları birimiyle iletişime geçebilirsiniz."
                );
                startInterviewButton.style.display = "none";
                return;
            }
            alert("Mülakat başlatılamadı. Lütfen tekrar deneyin veya İnsan Kaynakları birimiyle iletişime geçin.");
            interviewStarting = false;
            updateStartButton();
        });
});

updateStartButton();
if (interviewToken) {
    fetch(`/api/interview-links/${interviewToken}`)
        .then(response => response.json())
        .then(data => {
            console.log("Aday Bilgileri:", data);

    if (data.success) {
    const candidateGreeting = document.getElementById("candidateGreeting");
    const companyName = document.getElementById("companyName");

    if (candidateGreeting && data.candidate) {
        candidateGreeting.textContent = data.candidate.name || "Sayın Aday";
    }

    if (companyName && data.candidate) {
        companyName.textContent = data.candidate.company || "-";
    }

    if (data.expired === true || data.status === "expired") {
    // Süresi dolmuş adaylarda gereksiz bilgileri gizle
if (welcomeCard) {
    const infoGrid = welcomeCard.querySelector(".info-grid");
    const quietNote = welcomeCard.querySelector(".quiet-note");

    if (infoGrid) {
        infoGrid.style.display = "none";
    }

    if (quietNote) {
        quietNote.style.display = "none";
    }
}

if (welcomeCard) {
    const paragraphs = welcomeCard.querySelectorAll("p");

    paragraphs.forEach(p => {
        const text = p.textContent.trim();

        if (
            text.includes("8–10 dakika") ||
            text.includes("10 soru") ||
            text.includes("Lütfen sessiz")
        ) {
            p.style.display = "none";
        }
    });
}
    const welcomeTitle = document.getElementById("candidateGreeting");

if (welcomeTitle) {
    const heading = welcomeTitle.parentElement;

    if (heading) {
        heading.textContent = "Sayın ";

        const candidateName = document.createElement("span");
        candidateName.textContent = data.candidate?.name || "Aday";
        candidateName.style.color = "#3478e5";

        heading.appendChild(candidateName);
    }
}

const welcomeDescription = welcomeCard?.querySelector("p");

if (welcomeDescription) {
    welcomeDescription.textContent =
        "Mülakat bağlantınızın geçerlilik süresi sona ermiştir.";
}
        const expiredMessage = document.createElement("div");

        expiredMessage.style.cssText =
            "padding:24px;text-align:center;background:#fff;border-radius:12px;margin:20px 0;";

        expiredMessage.innerHTML = `
            <h2 style="color:#b91c1c;">Mülakat Bağlantınızın Süresi Doldu</h2>
            <p>Dijital ön mülakata başlamak için tanımlanan 8 saatlik süre sona ermiştir.</p>
            <p>Yeni bağlantı için başvurduğunuz kurumun İnsan Kaynakları birimiyle iletişime geçebilirsiniz.</p>
        `;

        const consentCard = document.getElementById("consentCard");
        const startButton = document.getElementById("startInterviewButton");

        if (consentCard) {
            consentCard.replaceWith(expiredMessage);
        }

        if (startButton) {
            startButton.disabled = true;
            startButton.style.display = "none";
        }
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

    fetch(`/api/interviews/${interviewToken}/answer`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
    answer: answer,
    question_number: currentQuestionNumber,
    question: questionText.textContent
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

        if (data.completed) {
            questionProgress.textContent = "Mülakat Tamamlandı";
            questionText.textContent =
                "Teşekkür ederiz. Mülakatınızı başarıyla tamamladınız. Yanıtlarınız değerlendirilmek üzere ilgili birime iletilmiştir.";

            answerText.value = "";
            answerText.style.display = "none";
            submitAnswerButton.style.display = "none";
            return;
        }

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