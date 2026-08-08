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

    alert("Mülakat ekranı bir sonraki adımda açılacaktır.");
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

            candidateGreeting.textContent = data.candidate.name;
            companyName.textContent = data.candidate.company;
})
        .catch(error => {
            console.error("Mülakat bilgisi alınamadı:", error);
        });
}