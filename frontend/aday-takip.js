const candidateList = document.getElementById("candidateList");
const candidateSummary = document.getElementById("candidateSummary");
const candidateSearch = document.getElementById("candidateSearch");
const statusFilter = document.getElementById("statusFilter");

let allCandidates = [];

const statusLabels = {
    waiting: "Bekliyor",
    opened: "Link Açıldı",
    started: "Mülakat Başladı",
    completed: "Tamamlandı",
    expired: "Süresi Doldu"
};

// ERNASA İK 1.1: Saatler Türkiye saatine göre gösterilir.
function candidateDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
}

function effectiveCandidateStatus(candidate) {
    const status = candidate.status === "created" ? "waiting" : candidate.status;
    if (status === "completed" || status === "expired") return status;
    const expiry = candidateDate(candidate.expires_at);
    return expiry && expiry.getTime() <= Date.now() ? "expired" : status;
}

function candidateDateTime(value) {
    const date = candidateDate(value);
    return date ? date.toLocaleString("tr-TR", {
        timeZone: "Europe/Istanbul", day: "2-digit", month: "2-digit",
        year: "numeric", hour: "2-digit", minute: "2-digit"
    }) : "-";
}

function candidateCreatedLabel(candidate) {
    if (candidate.created_at) return candidateDateTime(candidate.created_at);
    if (candidate.estimated_created_at) {
        return "≈ " + candidateDateTime(candidate.estimated_created_at) + " (tahmini)";
    }
    return "-";
}

function candidateRemainingLabel(candidate) {
    const status = effectiveCandidateStatus(candidate);
    if (status === "completed") return "Tamamlandı";
    if (status === "expired") return "Süresi doldu";
    const expiry = candidateDate(candidate.expires_at);
    if (!expiry) return "-";
    const seconds = Math.max(0, Math.floor((expiry.getTime() - Date.now()) / 1000));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    return [hours, minutes, secs].map(n => String(n).padStart(2, "0")).join(":");
}

function candidateEscape(value) {
    return String(value ?? "-").replace(/[&<>"']/g, char => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[char]));
}

async function loadCandidates() {
    try {
        const response = await fetch("/api/hr/interviews");
        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error("Adaylar alınamadı.");
        }

        allCandidates = data.interviews || [];
        updateCandidateStats();

        renderCandidates();

    } catch (error) {
        candidateList.innerHTML = "Aday kayıtları yüklenemedi.";
        console.error(error);
    }
}

let currentPage = 1;
const candidatesPerPage = 10;

function renderCandidates() {
    const searchValue = candidateSearch.value.toLowerCase().trim();
    const selectedStatus = statusFilter.value;

    const filteredCandidates = allCandidates.filter(candidate => {
        const searchableText = `
            ${candidate.token || ""}
            ${candidate.name || ""}
            ${candidate.company || ""}
            ${candidate.position || ""}
        `.toLowerCase();

        const matchesSearch = searchableText.includes(searchValue);

        const candidateStatus = effectiveCandidateStatus(candidate);

        const matchesStatus =
            !selectedStatus || candidateStatus === selectedStatus;

        return matchesSearch && matchesStatus;
    });

    candidateSummary.textContent =
        `Toplam aday: ${allCandidates.length} | Gösterilen: ${filteredCandidates.length}`;

    if (filteredCandidates.length === 0) {
        candidateList.innerHTML = `
            <div class="candidate-empty">
                Arama kriterlerine uygun aday bulunamadı.
            </div>
        `;
        return;
    }

    const totalPages = Math.ceil(
        filteredCandidates.length / candidatesPerPage
    );

    if (currentPage > totalPages) {
        currentPage = totalPages;
    }

    const startIndex = (currentPage - 1) * candidatesPerPage;
    const endIndex = startIndex + candidatesPerPage;

    const pageCandidates = filteredCandidates.slice(
        startIndex,
        endIndex
    );

    const rows = pageCandidates.map((candidate, index) => {

        const candidateStatus = effectiveCandidateStatus(candidate);

        const statusText =
            statusLabels[candidateStatus] || candidateStatus || "-";

        return `
            <tr>
                <td>${startIndex + index + 1}</td>

                <td>
                    <strong class="candidate-number">
                        ${candidateEscape(candidate.token)}
                    </strong>
                </td>

                <td>
                    ${candidateEscape(candidate.name || "Ad bilgisi yok")}
                </td>

                <td>
                    ${candidateEscape(candidate.position)}
                </td>

                <td>
                    ${candidateEscape(candidate.company)}
                </td>

                <td>
                    <span class="candidate-status status-${candidateStatus}">
                        ${statusText}
                    </span>
                </td>

                <td>
                    ${candidateEscape(candidateCreatedLabel(candidate))}
                </td>
                <td>${candidateEscape(candidateDateTime(candidate.expires_at))}</td>
                <td>${candidateEscape(candidateRemainingLabel(candidate))}</td>

                <td>
                    ${
                        candidate.status === "completed"
                            ? `
                                <button
                                    type="button"
                                    class="candidate-detail-button"
                                    onclick="openCandidateReport('${candidate.token}')"
                                >
                                    Detay
                                </button>
                              `
                            : `<span class="candidate-no-report">-</span>`
                    }
                </td>
            </tr>
        `;
    }).join("");

    candidateList.innerHTML = `
        <div class="candidate-table-header">
            <div>
                <h2>Adaylar</h2>
                <p>Sisteme eklenen adayların listesi ve mülakat durumları.</p>
            </div>

            <span>
                ${filteredCandidates.length} aday listeleniyor
            </span>
        </div>

        <div class="candidate-table-wrapper">
            <table class="candidate-table">
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Aday No</th>
                        <th>Ad Soyad</th>
                        <th>Pozisyon</th>
                        <th>Firma</th>
                        <th>Mülakat Durumu</th>
                        <th>Oluşturma Tarihi</th>
                        <th>Bitiş</th>
                        <th>Kalan Süre</th>
                        <th>İşlemler</th>
                    </tr>
                </thead>

                <tbody>
                    ${rows}
                </tbody>
            </table>
        </div>

        <div class="candidate-pagination">
            <span>
                ${startIndex + 1}-${Math.min(endIndex, filteredCandidates.length)}
                / ${filteredCandidates.length} kayıt
            </span>

            <div>
                <button
                    type="button"
                    onclick="changeCandidatePage(-1)"
                    ${currentPage === 1 ? "disabled" : ""}
                >
                    ‹
                </button>

                <strong>
                    ${currentPage} / ${totalPages}
                </strong>

                <button
                    type="button"
                    onclick="changeCandidatePage(1)"
                    ${currentPage === totalPages ? "disabled" : ""}
                >
                    ›
                </button>
            </div>
        </div>
    `;
}

function changeCandidatePage(direction) {
    currentPage += direction;
    renderCandidates();
}


function openCandidateReport(token) {
    const interviewsNav = document.querySelector('.sidebar-item[data-screen="interviews"]');

    if (interviewsNav) {
        interviewsNav.click();
    } else {
        showPortalScreen("interviews");
    }

    openInterviewReport(token);
}
function filterCandidatesByStatus(status) {
    statusFilter.value = status;
    currentPage = 1;
    renderCandidates();
}
candidateSearch.addEventListener("input", renderCandidates);
statusFilter.addEventListener("change", renderCandidates);

loadCandidates();

function updateCandidateStats() {
    document.getElementById("statTotal").textContent = allCandidates.length;

    document.getElementById("statWaiting").textContent =
        allCandidates.filter(candidate => effectiveCandidateStatus(candidate) === "waiting").length;

    document.getElementById("statOpened").textContent =
        allCandidates.filter(candidate => effectiveCandidateStatus(candidate) === "opened").length;

    document.getElementById("statStarted").textContent =
        allCandidates.filter(candidate => effectiveCandidateStatus(candidate) === "started").length;

    document.getElementById("statCompleted").textContent =
        allCandidates.filter(candidate => effectiveCandidateStatus(candidate) === "completed").length;

    document.getElementById("statExpired").textContent =
        allCandidates.filter(candidate => effectiveCandidateStatus(candidate) === "expired").length;
}
// Görünür geri sayım ve durumları düzenli yenile; API'deki kayıtlar değiştirilmez.
setInterval(() => {
    if (!candidateList || !candidateList.offsetParent) return;
    renderCandidates();
    updateCandidateStats();
}, 1000);
