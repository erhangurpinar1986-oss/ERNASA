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

        let candidateStatus = candidate.status;

        // Eski "created" kayıtlarını bekleyen aday olarak değerlendir
        if (candidateStatus === "created") {
            candidateStatus = "waiting";
        }

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

        let candidateStatus = candidate.status;

        if (candidateStatus === "created") {
            candidateStatus = "waiting";
        }

        const statusText =
            statusLabels[candidateStatus] || candidateStatus || "-";

        return `
            <tr>
                <td>${startIndex + index + 1}</td>

                <td>
                    <strong class="candidate-number">
                        ${candidate.token || "-"}
                    </strong>
                </td>

                <td>
                    ${candidate.name || "Ad bilgisi yok"}
                </td>

                <td>
                    ${candidate.position || "-"}
                </td>

                <td>
                    ${candidate.company || "-"}
                </td>

                <td>
                    <span class="candidate-status status-${candidateStatus}">
                        ${statusText}
                    </span>
                </td>

                <td>
                    ${formatCandidateDate(candidate.created_at)}
                </td>

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

function formatCandidateDate(dateValue) {
    if (!dateValue) {
        return "-";
    }

    const date = new Date(dateValue);

    if (Number.isNaN(date.getTime())) {
        return "-";
    }

    return date.toLocaleDateString("tr-TR");
}

function openCandidateReport(token) {
    showPortalScreen("interviews");
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
        allCandidates.filter(candidate => candidate.status === "waiting").length;

    document.getElementById("statOpened").textContent =
        allCandidates.filter(candidate => candidate.status === "opened").length;

    document.getElementById("statStarted").textContent =
        allCandidates.filter(candidate => candidate.status === "started").length;

    document.getElementById("statCompleted").textContent =
        allCandidates.filter(candidate => candidate.status === "completed").length;

    document.getElementById("statExpired").textContent =
        allCandidates.filter(candidate => candidate.status === "expired").length;
}