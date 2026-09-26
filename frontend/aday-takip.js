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

        renderCandidates();

    } catch (error) {
        candidateList.innerHTML = "Aday kayıtları yüklenemedi.";
        console.error(error);
    }
}

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

        const matchesStatus =
            !selectedStatus || candidate.status === selectedStatus;

        return matchesSearch && matchesStatus;
    });

    candidateSummary.textContent =
        `Toplam aday: ${allCandidates.length} | Gösterilen: ${filteredCandidates.length}`;

    if (filteredCandidates.length === 0) {
        candidateList.innerHTML = "Arama kriterlerine uygun aday bulunamadı.";
        return;
    }

   candidateList.innerHTML = filteredCandidates.map(candidate => `
    <div class="analysis-result" style="margin-top:12px;">
        <div style="
            display:flex;
            justify-content:space-between;
            align-items:flex-start;
            gap:16px;
            flex-wrap:wrap;
        ">
            <div>
                <strong style="font-size:16px;">
                    ${candidate.name || "Ad bilgisi yok"}
                </strong>

                <div style="margin-top:5px;">
                    <strong>Aday No:</strong> ${candidate.token}
                </div>

                <div style="margin-top:4px;">
                    <strong>Firma:</strong> ${candidate.company || "-"}
                </div>

                <div style="margin-top:4px;">
                    <strong>Pozisyon:</strong> ${candidate.position || "-"}
                </div>
            </div>

            <div>
                <strong>Durum:</strong>
                ${statusLabels[candidate.status] || candidate.status || "-"}
            </div>
        </div>

        ${candidate.status === "completed" ? `
            <button
                type="button"
                class="primary-button"
                style="margin-top:14px;"
                onclick="openCandidateReport('${candidate.token}')"
            >
                RAPORU GÖRÜNTÜLE
            </button>
        ` : ""}
    </div>
`).join("");
}

function openCandidateReport(token) {
    window.location.href = `/ik#${token}`;
}

candidateSearch.addEventListener("input", renderCandidates);
statusFilter.addEventListener("change", renderCandidates);

loadCandidates();