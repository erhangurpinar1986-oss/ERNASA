
function shieldEscapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    }[ch]));
}

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
        <strong>${shieldEscapeHtml(interview.token)}</strong><br>
        ${shieldEscapeHtml(interview.name || "Ad bilgisi yok")}<br>
        ${shieldEscapeHtml(interview.company || "-")} - ${shieldEscapeHtml(interview.position || "-")}<br>
        <strong>Durum:</strong> ${shieldEscapeHtml(statusLabels[interview.status] || interview.status || "-")}

        ${interview.status === "completed" ? `
            <div style="margin-top:12px;">
                <button
                    type="button"
                    class="primary-button"
                    data-report-token="${shieldEscapeHtml(interview.token)}"
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

document.addEventListener("DOMContentLoaded", async () => {
    if (!companyPortalPage || (await portalMetadataPromise)?.products?.includes("ai")) loadInterviewResults();
});

// setInterval(() => {
//     loadInterviewResults();
// }, 10000);


document.addEventListener("click", function(event) {
    const reportButton = event.target.closest("[data-report-token]");
    if (reportButton) {
        openInterviewReport(reportButton.dataset.reportToken);
        return;
    }

    const pdfButton = event.target.closest("[data-pdf-token]");
    if (pdfButton) {
        downloadInterviewPdf(pdfButton.dataset.pdfToken);
    }
});

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
        const jobFitAnalysis = (data.job_fit_analysis || "")
    .split(/\r?\n/)
    .map(line => line.trim())
    .join("\n")
    .trim();
        resultsBox.innerHTML = `
            <div style="
                border:1px solid #d9e2ec;
                border-radius:10px;
                padding:20px;
                background:#ffffff;
            ">

                <h3 style="margin-top:0;">
                    ${shieldEscapeHtml(interview.name || "Aday")} - Mülakat Raporu
                </h3>

                <p>
                    <strong>Aday No:</strong> ${shieldEscapeHtml(interview.token)}<br>
                    <strong>Firma:</strong> ${shieldEscapeHtml(interview.company || "-")}<br>
                    <strong>Pozisyon:</strong> ${shieldEscapeHtml(interview.position || "-")}<br>
                    <strong>Durum:</strong> ${shieldEscapeHtml(interview.status || "-")}
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

        <div style="line-height:1.6; text-align:left; overflow-wrap:anywhere;">
            ${renderJobFitAnalysisHtml(jobFitAnalysis)}
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
                        <strong>Soru ${shieldEscapeHtml(item.question_number)}</strong>

                        <p>
                            ${shieldEscapeHtml(item.question)}
                        </p>

                        <strong>Cevap</strong>

                        <p>
                            ${shieldEscapeHtml(item.answer)}
                        </p>
                    </div>
                `).join("")}
<button
    type="button"
    class="primary-button"
    style="margin-top:20px;"
    data-pdf-token="${shieldEscapeHtml(interview.token)}"
>
    PDF RAPORU İNDİR
</button>
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
        showAssessmentControls(resultsBox,token,data);

    } catch (error) {
        resultsBox.innerHTML = `
            <div class="analysis-empty">
                Mülakat raporu yüklenemedi.
            </div>
        `;

        console.error(error);
    }
}

function showAssessmentControls(root, token, data) {
    const status = {
        not_started:"Başlatılmadı",queued:"Sırada",running:"Devam ediyor",
        failed:"Başarısız",completed:"Tamamlandı"
    }[data.assessment_status];
    if (!status) return;
    const anchor = root.querySelector("hr");
    const label = document.createElement("p");
    label.textContent = "Değerlendirme durumu · " + status;
    anchor.before(label);
    const retry = data.can_retry_assessment;
    if (!retry && !["queued","running"].includes(data.assessment_status)) return;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "primary-button";
    button.textContent = retry ? (data.assessment_status === "failed" ?
        "Değerlendirmeyi yeniden dene" : "Değerlendirmeyi başlat") : "Durumu güncelle";
    const feedback = document.createElement("p");
    feedback.setAttribute("role","status");
    anchor.before(button,feedback);
    button.addEventListener("click", async () => {
        button.disabled = true;
        try {
            if (retry) await portalJson("/api/hr/interviews/"+encodeURIComponent(token)+"/assessment",
                {method:"POST"},"Değerlendirme başlatılamadı.");
            if (button.isConnected) await openInterviewReport(token);
        } catch (error) {
            feedback.textContent = error.message;
        } finally { button.disabled = false; }
    });
}

const companyPortalPage = location.hostname.endsWith(".ernasa.com") && location.hostname !== "www.ernasa.com";
const portalMetadataPromise = companyPortalPage
    ? fetch("/api/ik/portal", {cache: "no-store"}).then(response => response.ok ? response.json() : null).catch(() => null)
    : Promise.resolve(null);
if (companyPortalPage) {
    document.documentElement.classList.add("company-portal");
    document.getElementById("portalHomeLink")?.setAttribute("href", "/ik");
    document.getElementById("portalOverviewLink")?.setAttribute("href", "/ik");
    window.addEventListener("pagehide", event => {
        if (event.persisted) document.documentElement.style.visibility = "hidden";
    });
    window.addEventListener("pageshow", event => {
        if (event.persisted) window.location.reload();
    });
    const logo = document.getElementById("hrCompanyLogo");
    if (logo) {
        const fallback = document.getElementById("companyLogoPlaceholder");
        document.getElementById("companyLogoSlot").hidden = false;
        logo.onload = () => { logo.hidden = false; fallback.hidden = true; };
        logo.onerror = () => { logo.hidden = true; fallback.hidden = false; };
        logo.src = "/api/ik/company-logo";
    }
}

async function downloadInterviewPdf(token) {
    if (companyPortalPage) {
        const metadata = await fetch("/api/ik/portal");
        if (!metadata.ok || !(await metadata.json()).can_pdf) {
            alert("PDF raporu için yetkiniz yok.");
            return;
        }
        window.location.href = `/api/hr/interviews/${encodeURIComponent(token)}/pdf`;
        return;
    }
    const reportWindow = window.open("", "_blank");
    try {
        const response = await fetch(`/api/hr/interviews/${token}`);
        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error("Rapor bilgileri alınamadı.");
        }

        const interview = data.interview;
        const answers = data.answers || [];
        const jobFitAnalysis = (data.job_fit_analysis || "")
    .split(/\r?\n/)
    .map(line => line.trim())
    .join("\n")
    .trim();


        reportWindow.document.write(`
            <!DOCTYPE html>
            <html lang="tr">
            <head>
                <meta charset="UTF-8">
                <title>${shieldEscapeHtml(interview.name || "Aday")} - ERNASA Mülakat Raporu</title>

                <style>
                    body {
                        font-family: Arial, sans-serif;
                        color: #1f2937;
                        margin: 0;
                        padding: 35px;
                    }

                    .report-header {
                        display: flex;
                        justify-content: space-between;
                        align-items: center;
                        border-bottom: 2px solid #12377c;
                        padding-bottom: 15px;
                        margin-bottom: 25px;
                    }

                    .ernasa-logo {
                        width: 180px;
                        max-height: 80px;
                        object-fit: contain;
                    }

                    .company-logo {
                        width: 130px;
                        height: 70px;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        border: 1px dashed #cbd5e1;
                        color: #64748b;
                        font-size: 12px;
                    }

                    h1 {
                        color: #12377c;
                        font-size: 22px;
                        margin-bottom: 5px;
                    }

                    h2 {
                        color: #12377c;
                        font-size: 17px;
                        margin-top: 25px;
                    }

                    .candidate-info,
                    .analysis,
                    .answer {
                        border: 1px solid #d9e2ec;
                        border-radius: 8px;
                        padding: 15px;
                        margin-bottom: 15px;
                    }

                    .analysis {
                        white-space: normal; text-align: left; overflow-wrap: anywhere;
                        line-height: 1.6;
                        background: #f8fafc;
                    }

                    .analysis + h2 {
                        break-before: page;
                        page-break-before: always;
                    }
                    .answer {
                        page-break-inside: avoid;
                    }

                    .question {
                        font-weight: bold;
                        margin-bottom: 8px;
                    }

                    .footer {
                        margin-top: 35px;
                        padding-top: 12px;
                        border-top: 1px solid #d9e2ec;
                        text-align: center;
                        font-size: 11px;
                        color: #64748b;
                    }

                    @media print {
                        body {
                            padding: 10px;
                        }
                        .analysis section {
                            break-inside: avoid;
                            page-break-inside: avoid;
                        }
                        .analysis h4 {
                            break-after: avoid;
                            page-break-after: avoid;
                        }
                    }
                </style>
            </head>

            <body>

                <div class="report-header">
                    <img
                        src="/static/assets/ernasalogo.png"
                        class="ernasa-logo"
                        alt="ERNASA"
                    >

                    <div class="company-logo">
                        Firma Logosu
                    </div>
                </div>

                <h1>Yapay Zekâ Destekli Aday Değerlendirme Raporu</h1>

                <div class="candidate-info">
                    <strong>Aday:</strong> ${shieldEscapeHtml(interview.name || "-")}<br>
                    <strong>Aday No:</strong> ${shieldEscapeHtml(interview.token || "-")}<br>
                    <strong>Firma:</strong> ${shieldEscapeHtml(interview.company || "-")}<br>
                    <strong>Pozisyon:</strong> ${shieldEscapeHtml(interview.position || "-")}<br>
                    <strong>Durum:</strong> ${shieldEscapeHtml(interview.status || "-")}
                </div>

                <h2>AI İşe Uygunluk Analizi</h2>

                <div class="analysis">
                    ${renderJobFitAnalysisHtml(jobFitAnalysis)}
                </div>

                <h2>Mülakat Soru ve Cevapları</h2>

                ${answers.map(item => `
                    <div class="answer">
                        <div class="question">
                            Soru ${shieldEscapeHtml(item.question_number)}: ${shieldEscapeHtml(item.question || "")}
                        </div>

                        <div>
                            <strong>Cevap:</strong> ${shieldEscapeHtml(item.answer || "")}
                        </div>
                    </div>
                `).join("")}

                <div class="footer">
                    ERNASA Yapay Zekâ Destekli İnsan Kaynakları Sistemi<br>
                    Rapor Tarihi: ${new Date().toLocaleString("tr-TR")}
                </div>

                <script>
                    window.onload = function () {
                        setTimeout(function () {
                            window.print();
                        }, 500);
                    };
                <\/script>

            </body>
            </html>
        `);

        reportWindow.document.close();

    } catch (error) {
        console.error(error);
        alert("PDF raporu hazırlanırken bir hata oluştu.");
    }
}
function downloadBackendPdf(token) {
    window.location.href = `/api/hr/interviews/${token}/pdf`;
}

// ===============================
// ERNASA KONTROL PANELİ
// ===============================

async function loadDashboard() {
    const totalCandidates = document.getElementById("totalCandidates");
    const completedInterviews = document.getElementById("completedInterviews");
    const activeInterviews = document.getElementById("activeInterviews");
    const waitingEvaluation = document.getElementById("waitingEvaluation");
    const dashboardCandidateList = document.getElementById("dashboardCandidateList");

    // Dashboard bu sayfada yoksa hiçbir işlem yapma.
    if (!dashboardCandidateList) {
        return;
    }

    try {
        const response = await fetch(companyPortalPage ? "/api/ik/candidates" : "/api/hr/interviews");
        const data = await response.json();

        if (!response.ok) {
            throw new Error("Aday kayıtları alınamadı.");
        }

        // API doğrudan dizi veya interviews alanı döndürürse ikisini de destekle.
        const candidates = Array.isArray(data)
            ? data
            : (data.interviews || []);

        const completed = candidates.filter(
            candidate => candidate.status === "completed"
        ).length;

        const active = candidates.filter(
            candidate =>
                candidate.status === "opened" ||
                candidate.status === "started"
        ).length;

        const waiting = candidates.filter(
    candidate =>
        candidate.status === "created" ||
        candidate.status === "waiting"
).length;

        totalCandidates.textContent = candidates.length;
        completedInterviews.textContent = completed;
        activeInterviews.textContent = active;
        waitingEvaluation.textContent = waiting;

        if (candidates.length === 0) {
            dashboardCandidateList.innerHTML = `
                <tr>
                    <td colspan="6" class="dashboard-empty">
                        Henüz aday kaydı bulunmuyor.
                    </td>
                </tr>
            `;
            return;
        }

        // Tabloyu ernasa_v5_dashboard_only.js yonetiyor.

    } catch (error) {
        console.error("Dashboard yükleme hatası:", error);

        dashboardCandidateList.innerHTML = `
            <tr>
                <td colspan="6" class="dashboard-empty">
                    Aday kayıtları yüklenemedi.
                </td>
            </tr>
        `;
    }
}

function getDashboardStatus(status) {
    const statusLabels = {
        waiting: "Başlamadı",
        opened: "Açıldı",
        started: "Devam Ediyor",
        completed: "Tamamlandı",
        expired: "Süresi Doldu",
        created: "Başlamadı",
    };

    return statusLabels[status] || status || "-";
}

function formatDashboardDate(dateValue) {
    if (!dateValue) {
        return "-";
    }

    const date = new Date(dateValue);

    if (Number.isNaN(date.getTime())) {
        return "-";
    }

    return date.toLocaleDateString("tr-TR");
}

// ===============================
// ERNASA PORTAL EKRAN GEÇİŞLERİ
// ===============================

const dashboardHome = document.getElementById("dashboardHome");
const cvAnalysisScreen = document.getElementById("cvAnalysisScreen");
const candidatesScreen = document.getElementById("candidatesScreen");
const interviewsScreen = document.getElementById("interviewsScreen");
const personnelScreen = document.getElementById("personnelScreen");
const personnelEditor = document.getElementById("personnelEditor");
const personnelMessage = document.getElementById("personnelMessage");
const personnelCreateForm = document.getElementById("personnelCreate");
let canManagePersonnel = false;
let editingPersonnelId = null;

async function portalJson(path, options, fallback) {
    let response;
    try {
        response = await fetch(path, {cache: "no-store", ...options});
    } catch {
        throw new Error("Sunucuya bağlanılamadı. Lütfen tekrar deneyin.");
    }
    if (response.status === 401) {
        window.location.replace("/ik-giris");
        throw new Error("Oturum süresi doldu.");
    }
    const result = await response.json().catch(() => null);
    if (!response.ok || !result) {
        const detail = result?.detail;
        throw new Error(typeof detail === "string" && !["Not Found","Method Not Allowed","Internal Server Error","Forbidden","Unauthorized"].includes(detail)
            ? detail : fallback);
    }
    return result;
}

async function personnelRequest(path = "", options = {}) {
    return portalJson("/api/ik/personnel" + path, options, "Personel işlemi tamamlanamadı.");
}

function personnelDate(value) {
    return value ? new Date(value.replace(" ", "T") + "Z").toLocaleString("tr-TR") : "—";
}

async function resetPersonnelPassword(userId, button) {
    if (!window.confirm("Bu hesabın parolası yenilensin mi? Açık oturumları kapatılacak.")) return;
    button.disabled = true;
    try {
        await personnelRequest(`/${userId}/password-reset`, {method: "POST"});
        personnelMessage.textContent = "Parola kurulum bilgisi personelin ayrı teslim kanalına iletildi.";
    } catch (error) {
        personnelMessage.textContent = error.message;
    } finally {
        button.disabled = false;
    }
}

async function loadPersonnel() {
    personnelMessage.textContent = "";
    personnelEditor.hidden = true;
    try {
        const result = await personnelRequest();
        document.getElementById("personnelSummary").textContent =
            `Toplam ${result.total} · Aktif ${result.active} · Pasif ${result.inactive}`;
        const rows = document.getElementById("personnelRows");
        rows.replaceChildren();
        for (const person of result.users) {
            const cellRow = rows.insertRow();
            const roles = {supervisor: "Süpervizör", hr_manager: "İK Müdürü", hr_staff: "İK Personeli"};
            for (const value of [person.name, person.email, roles[person.role],
                                person.active ? "Aktif" : "Pasif", personnelDate(person.created_at),
                                personnelDate(person.last_login_at)]) {
                cellRow.insertCell().textContent = value;
            }
            const actions = cellRow.insertCell();
            if (person.role !== "supervisor") {
                const button = document.createElement("button");
                button.type = "button";
                button.textContent = "Düzenle";
                button.addEventListener("click", () => {
                    editingPersonnelId = person.id;
                    for (const field of ["name", "email", "role", "active"]) {
                        personnelEditor.elements.namedItem(field).value = person[field];
                    }
                    const grants = document.getElementById("personnelGrants");
                    grants.hidden = !(window.ikSaasAccess?.ats && person.role === "hr_staff");
                    for (const code of ["ats_stage", "ats_schedule"]) {
                        personnelEditor.elements.namedItem(code).checked = (person.permissions || "").split(",").includes(code);
                    }
                    personnelEditor.hidden = false;
                });
                const resetButton = document.createElement("button");
                resetButton.type = "button";
                resetButton.textContent = "Parola yenile";
                resetButton.addEventListener("click", () => resetPersonnelPassword(person.id, resetButton));
                actions.append(button, resetButton);
            } else {
                actions.textContent = "Süpervizör hesabı · Parola profilden değiştirilir";
            }
        }
    } catch (error) {
        document.getElementById("personnelRows").replaceChildren();
        document.getElementById("personnelSummary").textContent = "";
        personnelMessage.textContent = error.message;
    }
}

personnelEditor?.addEventListener("submit", async event => {
    event.preventDefault();
    const submit = personnelEditor.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
        const {name, email, role, active} = Object.fromEntries(new FormData(personnelEditor));
        const fields = {name, email, role, active: active === "1"};
        await personnelRequest(`/${editingPersonnelId}`, {
            method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(fields)
        });
        await loadPersonnel();
    } catch (error) {
        personnelMessage.textContent = error.message;
    } finally {
        submit.disabled = false;
    }
});

document.getElementById("personnelGrantsSave")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
        const permissions = ["ats_stage","ats_schedule"].filter(code => personnelEditor.elements.namedItem(code).checked);
        await personnelRequest(`/${editingPersonnelId}/permissions`, {
            method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({permissions})
        });
        await loadPersonnel();
    } catch (error) {
        personnelMessage.textContent = error.message;
    } finally {
        button.disabled = false;
    }
});

personnelCreateForm?.addEventListener("submit", async event => {
    event.preventDefault();
    const submit = personnelCreateForm.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
        const fields = Object.fromEntries(new FormData(personnelCreateForm));
        await personnelRequest("", {
            method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(fields)
        });
        personnelCreateForm.reset();
        document.getElementById("personnelCreateDetails").open = false;
        await loadPersonnel();
        personnelMessage.textContent = "Personel oluşturuldu. İlk giriş bilgisi ayrı teslim kanalına iletildi.";
    } catch (error) {
        personnelMessage.textContent = error.message;
    } finally {
        submit.disabled = false;
    }
});

if (companyPortalPage) {
    const companyNameField = document.getElementById("companyName");
    companyNameField.readOnly = true;
    companyNameField.placeholder = "Firma bilgisi yükleniyor";
    portalMetadataPromise.then(metadata => {
        canManagePersonnel = Boolean(metadata?.can_manage_personnel);
        if (canManagePersonnel) document.getElementById("personnelMenu").style.removeProperty("display");
        companyNameField.value = metadata?.company_name || "";
        if (metadata?.company_name) document.getElementById("portalCompanyTitle").textContent = metadata.company_name;
        if (metadata?.user_name) document.getElementById("portalUserName").textContent = metadata.user_name;
        const roles = {supervisor: "Süpervizör", hr_manager: "İK Müdürü", hr_staff: "İK Personeli"};
        if (roles[metadata?.role]) document.getElementById("portalUserRole").textContent = roles[metadata.role];
        companyNameField.placeholder = "Firma bilgisi alınamadı";
    }).catch(() => { companyNameField.placeholder = "Firma bilgisi alınamadı"; });
}

function showPortalScreen(screenName) {
    dashboardHome.style.display = "none";
    cvAnalysisScreen.style.display = "none";
    candidatesScreen.style.display = "none";
    interviewsScreen.style.display = "none";
    if (personnelScreen) personnelScreen.hidden = true;
    document.getElementById("positionsScreen").hidden = screenName !== "positions";
    document.getElementById("usageScreen").hidden = screenName !== "usage";

    if (screenName === "dashboard") {
        dashboardHome.style.display = "block";
    }

    if (screenName === "cv") {
        cvAnalysisScreen.style.display = "block";
    }
    if (screenName === "candidates") {
        candidatesScreen.style.display = "block";
    }

    if (screenName === "interviews") {
        interviewsScreen.style.display = "block";
    }
    if (screenName === "personnel" && canManagePersonnel) {
        personnelScreen.hidden = false;
        loadPersonnel();
    }
    document.dispatchEvent(new CustomEvent("ik-portal-screen", {detail: screenName}));
}
document.querySelectorAll(".sidebar-item[data-screen]").forEach(item => {
    item.addEventListener("click", event => {
        if (companyPortalPage && item.id === "portalOverviewLink") return;
        event.preventDefault();

        const screenName = item.dataset.screen;

        document.querySelectorAll(".sidebar-item").forEach(menuItem => {
            menuItem.classList.remove("active");
        });

        item.classList.add("active");

        showPortalScreen(screenName);
    });
});
