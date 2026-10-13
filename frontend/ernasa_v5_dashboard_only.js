let dashboardCandidatesCache = [];
const dashboardStatusNames = {waiting:"Başlamadı",created:"Başlamadı",opened:"Link Açıldı",started:"Devam Ediyor",completed:"Tamamlandı",expired:"Süresi Doldu"};
const dashboardColors = {waiting:"#1673ed",active:"#f6a21a",completed:"#19a663",expired:"#ef5363"};
function dashboardEscape(value) {
    return String(value ?? "-").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function dashboardStatusKey(status) { return status === "created" ? "waiting" : status; }
function getDashboardStatus(status) { return dashboardStatusNames[status] || status || "-"; }
function formatDashboardDate(value) {
    if (!value) return "-";
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? "-" : d.toLocaleDateString("tr-TR");
}
function dashboardDateLabel(candidate) {
    const value = candidate.created_at || candidate.estimated_created_at;
    if (!value) return "-";
    const date = dashboardDate(value);
    if (!date) return "-";
    const formatted = date.toLocaleString("tr-TR", {timeZone:"Europe/Istanbul",day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"});
    return candidate.created_at ? formatted : `≈ ${formatted} (tahmini)`;
}
function dashboardExpiryLabel(candidate) {
    const date = dashboardDate(candidate.expires_at);
    return date ? date.toLocaleString("tr-TR", {timeZone:"Europe/Istanbul",day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "-";
}
function dashboardRemainingLabel(candidate) {
    if (["completed", "expired"].includes(candidate.status)) return getDashboardStatus(candidate.status);
    const date = dashboardDate(candidate.expires_at);
    if (!date) return "-";
    const seconds = Math.floor((date.getTime() - Date.now())/1000);
    if (seconds <= 0) return "Süresi doldu";
    const h = Math.floor(seconds/3600), m = Math.floor((seconds%3600)/60), s = seconds%60;
    return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}
function dashboardDate(value) { const d = new Date(value || ""); return Number.isNaN(d.getTime()) ? null : d; }
function dashboardGoCandidates() {
    const nav = document.querySelector('.sidebar-item[data-screen="candidates"]');
    if (nav) nav.click();
}
function dashboardRender() {
    const query = (document.getElementById("dashboardSearch")?.value || "").toLocaleLowerCase("tr-TR").trim();
    const all = dashboardCandidatesCache;
    const count = key => all.filter(c => dashboardStatusKey(c.status) === key).length;
    const active = count("started") + count("opened");
    document.getElementById("totalCandidates").textContent = all.length;
    document.getElementById("completedInterviews").textContent = count("completed");
    document.getElementById("activeInterviews").textContent = active;
    document.getElementById("waitingEvaluation").textContent = count("waiting");
    const segments = [
        {label:"Henüz Başlamayan",n:count("waiting"),color:dashboardColors.waiting},
        {label:"Mülakat Aşamasında",n:active,color:dashboardColors.active},
        {label:"Tamamlanan",n:count("completed"),color:dashboardColors.completed},
        {label:"Süresi Dolan",n:count("expired"),color:dashboardColors.expired}
    ];
    let angle=0;
    const slices=segments.filter(s=>s.n).map(s=>{const a=angle;angle+=s.n/Math.max(all.length,1)*100;return `${s.color} ${a}% ${angle}%`;});
    document.getElementById("dashboardDonut").style.background = slices.length ? `conic-gradient(${slices.join(",")})` : "#e5edf8";
    document.getElementById("dashboardDonutTotal").textContent=all.length;
    document.getElementById("dashboardStatusLegend").innerHTML=segments.map(s=>`<div><i style="background:${s.color}"></i><span>${s.label}</span><strong>${s.n}</strong></div>`).join("");
    const monthly = new Map();
    all.forEach(c=>{const d=dashboardDate(c.created_at);if(d){const k=`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}`;monthly.set(k,(monthly.get(k)||0)+1);}});
    const months=[...monthly].sort((a,b)=>a[0].localeCompare(b[0])).slice(-6);
    const trend=document.getElementById("dashboardTrend");
    if(!months.length){trend.innerHTML='<p class="v5-empty">Kayıt tarihi verisi bulunmuyor.</p>';} else {
        const max=Math.max(...months.map(x=>x[1]),1), width=320,height=160;
        const points=months.map((m,i)=>({x:20+i*(width-40)/Math.max(months.length-1,1),y:height-32-m[1]/max*105}));
        const poly=points.map(p=>`${p.x},${p.y}`).join(" ");
        const labels=months.map((m,i)=>{const [y,mo]=m[0].split('-');return `<text x="${points[i].x}" y="${height-7}" text-anchor="middle">${mo}/${y.slice(2)}</text>`;}).join('');
        trend.innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Aylık aday sayıları" preserveAspectRatio="xMidYMid meet"><line x1="10" y1="${height-32}" x2="310" y2="${height-32}" stroke="#e3eaf4"/><polyline points="${poly}" fill="none" stroke="#1673ed" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>${points.map((p,i)=>`<circle cx="${p.x}" cy="${p.y}" r="4" fill="#1673ed"><title>${months[i][0]}: ${months[i][1]} aday</title></circle>`).join('')}${labels}</svg>`;
    }
    const positions=new Map();all.forEach(c=>{const name=(c.position||"Belirtilmemiş").trim()||"Belirtilmemiş";positions.set(name,(positions.get(name)||0)+1);});
    const sorted=[...positions].sort((a,b)=>b[1]-a[1]);const shown=sorted.slice(0,5);const rest=sorted.slice(5).reduce((n,x)=>n+x[1],0);if(rest)shown.push(["Diğer",rest]);
    document.getElementById("dashboardPositions").innerHTML=shown.length?shown.map(([name,n])=>`<div class="v5-position"><span title="${dashboardEscape(name)}">${dashboardEscape(name)}</span><div class="v5-bar"><i style="width:${n/Math.max(all.length,1)*100}%"></i></div><strong>${n}</strong></div>`).join(''):'<p class="v5-empty">Henüz pozisyon kaydı yok.</p>';
    const ordered=[...all].sort((a,b)=>(dashboardDate(b.created_at)?.getTime()||0)-(dashboardDate(a.created_at)?.getTime()||0));
    const filtered=ordered.filter(c=>[c.name,c.token,c.position,c.email].some(v=>String(v||"").toLocaleLowerCase("tr-TR").includes(query)));
    const recent=document.getElementById("dashboardRecent");
    recent.innerHTML=filtered.length?filtered.slice(0,5).map(c=>`<div class="v5-recent-item"><span class="v5-initials">${dashboardEscape((c.name||"A").trim().slice(0,1).toLocaleUpperCase("tr-TR"))}</span><div class="v5-recent-name"><strong>${dashboardEscape(c.name||"Ad bilgisi yok")}</strong><small>${dashboardEscape(c.position||"Pozisyon belirtilmemiş")}</small></div><span class="v5-status v5-${dashboardEscape(dashboardStatusKey(c.status))}">${dashboardEscape(getDashboardStatus(c.status))}</span></div>`).join(''):'<p class="v5-empty">Aramaya uygun aday bulunamadı.</p>';
    const list=document.getElementById("dashboardCandidateList");
    list.innerHTML=filtered.length?filtered.slice(0,5).map(c=>`<tr><td><strong class="v5-token">${dashboardEscape(c.token)}</strong></td><td>${dashboardEscape(c.name||"Ad bilgisi yok")}</td><td>${dashboardEscape(c.position)}</td><td><span class="v5-status v5-${dashboardEscape(dashboardStatusKey(c.status))}">${dashboardEscape(getDashboardStatus(c.status))}</span></td><td><span title="${c.created_at ? 'Kesin oluşturulma zamanı' : 'Bitiş zamanından tahmini hesaplandı'}">${dashboardEscape(dashboardDateLabel(c))}</span></td><td>${dashboardEscape(dashboardExpiryLabel(c))}</td><td class="v5-remaining" data-expiry="${dashboardEscape(c.expires_at||'')}" data-status="${dashboardEscape(c.status||'')}">${dashboardEscape(dashboardRemainingLabel(c))}</td><td><button type="button" class="v5-text-btn" data-candidate-detail="${dashboardEscape(c.token)}">Detay →</button></td></tr>`).join(''):'<tr><td colspan="8" class="dashboard-empty">Aramaya uygun aday bulunamadı.</td></tr>';
}
async function loadDashboard() {
    const list=document.getElementById("dashboardCandidateList");
    if(!list)return;
    try {
        const response=await fetch("/api/hr/interviews");
        if(!response.ok)throw new Error("Aday kayıtları alınamadı.");
        const data=await response.json();
        dashboardCandidatesCache=Array.isArray(data)?data:(Array.isArray(data.interviews)?data.interviews:[]);
        dashboardRender();
    } catch(err) {
        console.error("Dashboard yükleme hatası:",err);
        list.innerHTML='<tr><td colspan="8" class="dashboard-empty">Aday kayıtları yüklenemedi.</td></tr>';
    }
}
document.getElementById("dashboardSearch")?.addEventListener("input",dashboardRender);
document.getElementById("dashboardRefresh")?.addEventListener("click",loadDashboard);
setInterval(() => {
    document.querySelectorAll(".v5-remaining").forEach(cell => {
        cell.textContent = dashboardRemainingLabel({expires_at:cell.dataset.expiry,status:cell.dataset.status});
    });
}, 1000);
document.addEventListener("click", event => {
    const detailButton = event.target.closest("[data-candidate-detail]");

    if (detailButton) {
        const token = detailButton.dataset.candidateDetail;
        const candidate = dashboardCandidatesCache.find(c => c.token === token);

        if (!candidate) return;

        if (candidate.status === "completed") {
            openCandidateReport(token);
        } else {
            dashboardGoCandidates();

            const search = document.getElementById("candidateSearch");
            const filter = document.getElementById("statusFilter");

            if (search && filter) {
                filter.value = "";
                search.value = token;
                search.dispatchEvent(new Event("input", { bubbles: true }));
            }
        }
        return;
    }

    if (event.target.closest("[data-go-candidates]")) {
        dashboardGoCandidates();
    }
});
loadDashboard();

// ===============================
// ERNASA PORTAL EKRAN GEÇİŞLERİ
// ===============================

