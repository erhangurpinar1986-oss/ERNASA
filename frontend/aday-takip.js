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
        <div>
            <strong>${candidate.token}</strong><br>
            ${candidate.name || "Ad bilgisi yok"}<br>
            ${candidate.company || "-"} - ${candidate.position || "-"}<br>
            <strong>Durum:</strong>
            ${statusLabels[candidate.status] || candidate.status || "-"}
            <br><br>

            ${candidate.status === "completed" ? `
                <button
                    type="button"
                    onclick="openCandidateReport('${candidate.token}')"
                >
                    Raporu Görüntüle
                </button>
            ` : ""}

            <hr>
        </div>
    `).join("");
}

function openCandidateReport(token) {
    window.location.href = `/ik#${token}`;
}

candidateSearch.addEventListener("input", renderCandidates);
statusFilter.addEventListener("change", renderCandidates);

loadCandidates();