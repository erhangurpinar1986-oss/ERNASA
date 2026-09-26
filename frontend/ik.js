const cvFileInput = document.getElementById("cvFile");
const uploadArea = document.querySelector(".upload-area");
const fileStatus = document.getElementById("fileStatus");
const analyzeButton = document.getElementById("analyzeButton");
const analysisBox = document.getElementById("analysisBox");
const createLinkButton = document.getElementById("createLinkButton");

let selectedFile = null;
let currentCvText = "";
const allowedExtensions = ["pdf", "doc", "docx"];
const maximumFileSize = 10 * 1024 * 1024;

function getFileExtension(fileName) {
    return fileName.split(".").pop().toLowerCase();
}

function formatFileSize(bytes) {
    if (bytes < 1024) {
        return `${bytes} B`;
    }

    if (bytes < 1024 * 1024) {
        return `${(bytes / 1024).toFixed(1)} KB`;
    }

    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function resetFileSelection(message) {
    selectedFile = null;
    cvFileInput.value = "";
    analyzeButton.disabled = true;
    createLinkButton.disabled = true;

    fileStatus.textContent = message;
    fileStatus.classList.remove("file-success");
    fileStatus.classList.add("file-error");
}

function handleFile(file) {
    if (!file) {
        return;
    }

    const extension = getFileExtension(file.name);

    if (!allowedExtensions.includes(extension)) {
        resetFileSelection(
            "Geçersiz dosya türü. Lütfen PDF, DOC veya DOCX yükleyiniz."
        );
        return;
    }

    if (file.size > maximumFileSize) {
        resetFileSelection(
            "Dosya boyutu çok büyük. En fazla 10 MB yükleyebilirsiniz."
        );
        return;
    }

    selectedFile = file;

    fileStatus.innerHTML = `
        <strong>✓ CV başarıyla seçildi</strong><br>
        ${file.name} — ${formatFileSize(file.size)}
    `;

    fileStatus.classList.remove("file-error");
    fileStatus.classList.add("file-success");

    analyzeButton.disabled = false;

    analysisBox.innerHTML = `
        <div class="analysis-empty">
            CV hazır. Analizi başlatmak için
            <strong>CV'Yİ ANALİZ ET</strong> butonuna basınız.
        </div>
    `;
}

cvFileInput.addEventListener("change", event => {
    const file = event.target.files[0];
    handleFile(file);
});

["dragenter", "dragover"].forEach(eventName => {
    uploadArea.addEventListener(eventName, event => {
        event.preventDefault();
        event.stopPropagation();
        uploadArea.classList.add("drag-active");
    });
});

["dragleave", "drop"].forEach(eventName => {
    uploadArea.addEventListener(eventName, event => {
        event.preventDefault();
        event.stopPropagation();
        uploadArea.classList.remove("drag-active");
    });
});

uploadArea.addEventListener("drop", event => {
    const file = event.dataTransfer.files[0];
    handleFile(file);
});

window.addEventListener("dragover", event => {
    event.preventDefault();
});

window.addEventListener("drop", event => {
    event.preventDefault();
});

analyzeButton.addEventListener("click", async () => {
    if (!selectedFile) {
        return;
    }

    const formData = new FormData();
formData.append("cv", selectedFile);

const uploadResponse = await fetch(
    "/api/cv/upload",
    {
        method: "POST",
        body: formData
    }
);

const uploadResult = await uploadResponse.json();

currentCvText = uploadResult.text || "";

if (!uploadResponse.ok) {
    throw new Error(
        uploadResult.detail || "CV backend'e yüklenemedi."
    );
}
document.getElementById("candidateName").value =
    uploadResult.contact?.name || "";

document.getElementById("candidatePhone").value =
    uploadResult.contact?.phone || "";

document.getElementById("candidateEmail").value =
    uploadResult.contact?.email || "";
    analysisBox.innerHTML = `
        <div class="analysis-progress">
            <strong>CV analiz ediliyor...</strong>

            <div class="progress-track">
                <div class="progress-bar" id="progressBar"></div>
            </div>

            <div id="analysisStatus">
                Dosya hazırlanıyor...
            </div>
        </div>
    `;

    analyzeButton.disabled = true;

    const progressBar = document.getElementById("progressBar");
    const analysisStatus = document.getElementById("analysisStatus");

    const steps = [
        { progress: 15, text: "CV metni okunuyor..." },
        { progress: 35, text: "İletişim bilgileri aranıyor..." },
        { progress: 55, text: "Eğitim bilgileri inceleniyor..." },
        { progress: 75, text: "İş deneyimleri inceleniyor..." },
        { progress: 90, text: "Yetkinlikler belirleniyor..." },
        { progress: 100, text: "Analiz tamamlandı." }
    ];

    let index = 0;

    const interval = setInterval(() => {
        const step = steps[index];

        progressBar.style.width = `${step.progress}%`;
        analysisStatus.textContent = step.text;

        index += 1;

        if (index === steps.length) {
            clearInterval(interval);

            setTimeout(() => {
                analysisBox.innerHTML = `
                    <div class="analysis-results">
                        <p>✓ CV dosyası okundu.</p>
                        <p>✓ İletişim bilgileri tarandı.</p>
                        <p>✓ Eğitim bilgileri tarandı.</p>
                        <p>✓ İş deneyimleri tarandı.</p>
                        <p>✓ Yetkinlikler tarandı.</p>

                        <small>
                            Gerçek bilgi çıkarma özelliği sunucu bağlantısı
                            eklendiğinde aktif olacaktır.
                        </small>
                    </div>
                `;

                createLinkButton.disabled = false;
                analyzeButton.disabled = false;
            }, 400);
        }
    }, 650);
});
createLinkButton.addEventListener("click", async () => {
    const candidate = {
        name: document.getElementById("candidateName").value.trim(),
        phone: document.getElementById("candidatePhone").value.trim(),
        email: document.getElementById("candidateEmail").value.trim(),
        company: document.getElementById("companyName").value.trim(),
        position: document.getElementById("positionName").value.trim(),
        cv_text: currentCvText
    };        
        if (candidate.position.length < 3) {
    alert("Lütfen geçerli bir pozisyon giriniz.");
    return;
}


       if (
       !candidate.name ||
       !candidate.phone ||
       !candidate.company ||
       !candidate.position
) {
        alert("Aday bilgileri, firma ve pozisyon alanlarını doldurunuz.");
        return;
    }

    try {
        createLinkButton.disabled = true;
        createLinkButton.textContent = "LİNK OLUŞTURULUYOR...";

        const response = await fetch(
            "/api/interview-links",
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(candidate)
            }
        );

        const result = await response.json();

        if (!response.ok) {
            throw new Error(
                result.detail || "Mülakat linki oluşturulamadı."
            );
        }

        const interviewActions =
            document.getElementById("interviewActions");

        const interviewLink =
            document.getElementById("interviewLink");

        const candidateNumber =
            document.getElementById("candidateNumber");

        const candidateStatus =
            document.getElementById("candidateStatus");

        interviewLink.value = result.interview_url;
        candidateNumber.textContent = result.candidate_number;
        candidateStatus.textContent = "Durum: Bekliyor";

        interviewActions.style.display = "block";

        console.log("Aday No:", result.candidate_number);
        console.log("Mülakat linki:", result.interview_url);
        console.log("Geçerlilik süresi:", result.expires_at);

    } catch (error) {
        alert(error.message);

    } finally {
        createLinkButton.disabled = false;
        createLinkButton.textContent = "MÜLAKAT LİNKİ OLUŞTUR";
    }
});


const copyLinkButton =
    document.getElementById("copyLinkButton");

copyLinkButton.addEventListener("click", async () => {
    const interviewLink =
        document.getElementById("interviewLink");

    try {
        await navigator.clipboard.writeText(
            interviewLink.value
        );

        copyLinkButton.textContent = "KOPYALANDI ✓";

        setTimeout(() => {
            copyLinkButton.textContent = "📋 Linki Kopyala";
        }, 2000);

    } catch (error) {
        alert("Link kopyalanamadı.");
    }
});


const whatsappButton =
    document.getElementById("whatsappButton");
whatsappButton.addEventListener("click", () => {
    const name =
        document.getElementById("candidateName").value.trim();

    const phone =
        document.getElementById("candidatePhone")
            .value
            .replace(/\D/g, "");

    const company =
        document.getElementById("companyName").value.trim();

    const position =
        document.getElementById("positionName").value.trim();

    const interviewLink =
        document.getElementById("interviewLink").value;

    if (!phone) {
        alert("Adayın telefon numarası bulunamadı.");
        return;
    }

    if (!interviewLink) {
        alert("Önce mülakat linki oluşturun.");
        return;
    }

    let whatsappPhone = phone;

    if (whatsappPhone.startsWith("0")) {
        whatsappPhone = "90" + whatsappPhone.substring(1);
    }

    const message =
        `Merhaba ${name},\n\n` +
        `${company} bünyesindeki ${position} pozisyonu için ` +
        `online mülakat bağlantınız oluşturulmuştur.\n\n` +
        `Mülakatınızı aşağıdaki bağlantı üzerinden ` +
        `8 saat içerisinde tamamlamanızı rica ederiz.\n\n` +
        `${interviewLink}\n\n` +
        `Başarılar dileriz.\n\n` +
        `ERNASA Yapay Zekâ İnsan Kaynakları Asistanı`;

    const whatsappUrl =
        `https://wa.me/${whatsappPhone}` +
        `?text=${encodeURIComponent(message)}`;

    window.open(whatsappUrl, "_blank");
});

    
const emailButton =
    document.getElementById("emailButton");

emailButton.addEventListener("click", () => {
    const name =
        document.getElementById("candidateName").value.trim();

    const email =
        document.getElementById("candidateEmail").value.trim();

    const company =
        document.getElementById("companyName").value.trim();

    const position =
        document.getElementById("positionName").value.trim();

    const interviewLink =
        document.getElementById("interviewLink").value;

    const subject =
        `${company} - ${position} Mülakat Daveti`;

    const body =
        `Merhaba ${name},\n\n` +
        `${company} bünyesindeki ${position} pozisyonu için ` +
        `online mülakat bağlantınız oluşturulmuştur.\n\n` +
        `${interviewLink}\n\n` +
        `Mülakatınızı 8 saat içerisinde tamamlamanızı rica ederiz.\n\n` +
        `İyi çalışmalar.`;

    const url =
        `mailto:${email}` +
        `?subject=${encodeURIComponent(subject)}` +
        `&body=${encodeURIComponent(body)}`;

    window.location.href = url;
});
async function loadInterviewResults() {
    const resultsBox = document.getElementById("interviewResults");

    if (!resultsBox) {
        return;
    }

    try {
        const response = await fetch("/api/hr/interviews");
        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error("Mülakat kayıtları alınamadı.");
        }

        if (!data.interviews || data.interviews.length === 0) {
            resultsBox.innerHTML = `
                <div class="analysis-empty">
                    Henüz mülakat kaydı bulunmuyor.
                </div>
            `;
            return;
        }
const statusLabels = {
    waiting: "Bekliyor",
    opened: "Link Açıldı",
    started: "Mülakat Başladı",
    completed: "Tamamlandı",
    expired: "Süresi Doldu"
};
        resultsBox.innerHTML = data.interviews.map(interview => `

    <div style="
        border:1px solid #e0e0e0;
        border-radius:10px;
        padding:16px;
        margin-bottom:12px;
        background:#ffffff;
    ">
        <strong>${interview.token}</strong><br>
        ${interview.name || "Ad bilgisi yok"}<br>
        ${interview.company || "-"} - ${interview.position || "-"}<br>
        <strong>Durum:</strong> ${statusLabels[interview.status] || interview.status || "-"}

        ${interview.status === "completed" ? `
            <div style="margin-top:12px;">
                <button
                    type="button"
                    class="primary-button"
                    onclick="openInterviewReport('${interview.token}')"
                >
                    RAPORU GÖRÜNTÜLE
                </button>
            </div>
        ` : ""}
    </div>
`).join("");


    } catch (error) {
        resultsBox.innerHTML = `
            <div class="analysis-empty">
                Mülakat kayıtları yüklenemedi.
            </div>
        `;

        console.error(error);
    }
}

loadInterviewResults();

setInterval(() => {
    loadInterviewResults();
}, 10000);

async function openInterviewReport(token) {
    const resultsBox = document.getElementById("interviewResults");

    try {
        resultsBox.innerHTML = `
            <div class="analysis-empty">
                Mülakat raporu yükleniyor...
            </div>
        `;

        const response = await fetch(`/api/hr/interviews/${token}`);
        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error("Mülakat raporu alınamadı.");
        }

        const interview = data.interview;
        const answers = data.answers || [];
        const jobFitAnalysis = (data.job_fit_analysis || "").trim();
        resultsBox.innerHTML = `
            <div style="
                border:1px solid #d9e2ec;
                border-radius:10px;
                padding:20px;
                background:#ffffff;
            ">

                <h3 style="margin-top:0;">
                    ${interview.name || "Aday"} - Mülakat Raporu
                </h3>

                <p>
                    <strong>Aday No:</strong> ${interview.token}<br>
                    <strong>Firma:</strong> ${interview.company || "-"}<br>
                    <strong>Pozisyon:</strong> ${interview.position || "-"}<br>
                    <strong>Durum:</strong> ${interview.status || "-"}
                </p>

                <hr>
${jobFitAnalysis ? `
    <div style="
        margin:20px 0;
        padding:18px;
        border:1px solid #d9e2ec;
        border-radius:10px;
        background:#f8fafc;
    ">
        <h3 style="margin-top:0;">AI İşe Uygunluk Analizi</h3>

        <div style="white-space:pre-wrap; line-height:1.6;">
            ${jobFitAnalysis}
        </div>
    </div>

    <hr>
` : ""}
                ${answers.map(item => `
                    <div style="
                        margin-top:18px;
                        padding:15px;
                        border:1px solid #e0e0e0;
                        border-radius:8px;
                    ">
                        <strong>Soru ${item.question_number}</strong>

                        <p>
                            ${item.question}
                        </p>

                        <strong>Cevap</strong>

                        <p>
                            ${item.answer}
                        </p>
                    </div>
                `).join("")}

                <button
                    type="button"
                    class="primary-button"
                    style="margin-top:20px;"
                    onclick="loadInterviewResults()"
                >
                    LİSTEYE DÖN
                </button>

            </div>
        `;

    } catch (error) {
        resultsBox.innerHTML = `
            <div class="analysis-empty">
                Mülakat raporu yüklenemedi.
            </div>
        `;

        console.error(error);
    }
}