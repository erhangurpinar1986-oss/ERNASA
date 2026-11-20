"use strict";

window.ikSaasAccess = {ats:false, ai:false, manager:false, report:false};

if (companyPortalPage) {
    const byId = id => document.getElementById(id);
    const positionForm = byId("positionForm");
    const positionTemplate = byId("positionTemplate");
    const templates = {
        warehouse:{name:"Depo Personeli",job_family:"Lojistik ve Depo",worker_group:"Operasyon",
            matching_criteria:"Yetkinlik | Depo planlama | 60 | depo planlama\nYetkinlik | Stok yönetimi | 40 | stok yönetimi"},
        accounting:{name:"Muhasebe Uzmanı",job_family:"Finans ve Muhasebe",worker_group:"Uzman",
            matching_criteria:"Yetkinlik | Muhasebe kayıtları | 60 | muhasebe kayitlari\nYetkinlik | Finansal raporlama | 40 | finansal raporlama"},
    };
    const cvMatchInput = positionForm.elements.namedItem("matching_criteria");
    const cvMatchSummary = byId("cvMatchCriteriaSummary");
    function cvMatchRows() {
        return cvMatchInput.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    }
    function updateCvMatchSummary() {
        const rows = cvMatchRows();
        const parsed = rows.map(line => line.split("|").map(value => value.trim()));
        const valid = parsed.every(parts => parts.length === 4 &&
            ["Yetkinlik","Mesleki deneyim","Eğitim","Sertifika"].includes(parts[0]) &&
            parts[1].length >= 2 && parts[1].length <= 120 &&
            /^(?:[1-9]|[1-9][0-9]|100)$/.test(parts[2]) &&
            parts[3].split(",").every(value => value.trim().length >= 2));
        const total = parsed.reduce((sum,parts) => sum + (Number(parts[2]) || 0),0);
        cvMatchSummary.textContent = !rows.length ?
            "Henüz kriter tanımlanmadı; CV eşleşme yüzdesi hesaplanamaz." :
            !valid ? "Kriter biçimini kontrol edin: kategori | kriter | ağırlık | kanıt ifadeleri." :
            rows.length + " kriter · toplam ağırlık " + total +
            (total === 100 ? " / 100 · Hazır." :
                " · Ağırlıkların toplamını 100 yapmanız önerilir.");
        cvMatchSummary.style.color = !rows.length || !valid ? "#a44a20" : "";
        return valid;
    }
    cvMatchInput.addEventListener("input",updateCvMatchSummary);
    byId("cvMatchAddCriteria").addEventListener("click", () => {
        const category = byId("cvMatchCategory").value;
        const label = byId("cvMatchRequirement").value.trim();
        const weight = byId("cvMatchWeight").value.trim();
        const signals = byId("cvMatchSignals").value.split(",").map(v => v.trim()).filter(Boolean);
        const invalid = !label || label.length < 2 || label.includes("|") ||
            !/^(?:[1-9]|[1-9][0-9]|100)$/.test(weight) ||
            signals.length < 1 || signals.length > 8 ||
            signals.some(v => v.length < 2 || v.length > 100 || v.includes("|"));
        if (invalid) {
            cvMatchSummary.textContent = "Bir kriter adı (en az 2 karakter), 1–100 ağırlık ve 1–8 kanıt ifadesi girin.";
            cvMatchSummary.style.color = "#a44a20";
            return;
        }
        const row = [category,label,weight,signals.join(", ")].join(" | ");
        const lines = cvMatchRows();
        if (lines.length >= 30 || (cvMatchInput.value.length + row.length + 1) > 6000) {
            cvMatchSummary.textContent = "En fazla 30 kriter ve 6000 karakter ekleyebilirsiniz.";
            cvMatchSummary.style.color = "#a44a20";
            return;
        }
        lines.push(row);
        cvMatchInput.value = lines.join("\n");
        byId("cvMatchRequirement").value = "";
        byId("cvMatchSignals").value = "";
        updateCvMatchSummary();
        byId("cvMatchRequirement").focus();
    });
    updateCvMatchSummary();
    const candidateForm = byId("atsCandidateForm");
    const managerForm = byId("candidateManagerForm");
    let editingPosition = "";
    let managingCandidate = "";

    async function api(path, method = "GET", data) {
        const options = {method, cache:"no-store"};
        if (data !== undefined) {
            options.headers = {"Content-Type":"application/json"};
            options.body = JSON.stringify(data);
        }
        return portalJson("/api/ik/" + path, options, "İşlem tamamlanamadı.");
    }

    function options(select, rows, placeholder) {
        const old = select.value;
        select.replaceChildren(new Option(placeholder, ""));
        for (const row of rows) select.add(new Option(row.name, row.id));
        if (rows.some(row => String(row.id) === old)) select.value = old;
    }

    async function loadPositions() {
        const notice = byId("positionsMessage");
        try {
            const [{positions},{departments}] = await Promise.all([api("positions"),api("departments")]);
            options(positionForm.elements.namedItem("department_id"), departments, "Departman seçin");
            const open = positions.filter(row => row.open);
            options(candidateForm.elements.namedItem("position_id"), open, "Açık pozisyon seçin");
            const rows = byId("positionsRows");
            rows.replaceChildren();
            for (const position of positions) {
                const row = rows.insertRow();
                for (const value of [position.name, position.department, position.location,
                    position.open ? "Açık" : "Kapalı", new Date(position.created_at).toLocaleDateString("tr-TR"),
                    position.candidate_count]) {
                    row.insertCell().textContent = value || (value === 0 ? "0" : "—");
                }
                const actions = row.insertCell();
                const applicants = document.createElement("button");
                applicants.type = "button";
                applicants.textContent = "İlgili Adayları Gör";
                applicants.addEventListener("click", () => {
                    const menu = byId("poolMenu");
                    menu.dataset.filterPosition = position.id;
                    menu.click();
                });
                actions.append(applicants);
                if (!window.ikSaasAccess.manager) continue;
                const edit = document.createElement("button");
                edit.type = "button";
                edit.textContent = "Düzenle";
                edit.addEventListener("click", () => {
                    editingPosition = position.id;
                    positionTemplate.hidden = true;
                    byId("positionEditor").hidden = false;
                    byId("positionEditorTitle").textContent = "Pozisyonu düzenle";
                    for (const field of positionForm.elements) {
                        if (field.name) field.value = position[field.name] ?? (field.name === "openings" ? 1 : "");
                    }
                    updateCvMatchSummary();
                    positionForm.elements.namedItem("name").focus();
                });
                const members = document.createElement("button");
                members.type = "button";
                members.textContent = "Personel yetkisi";
                members.addEventListener("click", () => loadPositionMembers(position));
                actions.append(edit, members);
                if (position.state !== "closed") {
                    const close = document.createElement("button");
                    close.type = "button";
                    close.textContent = "Kapat";
                    close.addEventListener("click", async () => {
                        if (!window.confirm("Pozisyon kapatılsın mı? Mevcut başvurular korunacak.")) return;
                        try {
                            await api("positions/" + position.id + "/close","POST");
                            await loadPositions();
                        } catch (error) { notice.textContent = error.message; }
                    });
                    actions.append(close);
                }
            }
            const status = positionForm.querySelector("button[type=submit]");
            byId("positionDepartmentLink").hidden = departments.length>0 || !window.ikSaasAccess.manager;
            status.disabled = !departments.length;
            notice.textContent = !departments.length ? "Pozisyon açmak için Firma Ayarları’ndan departman ekleyin."
                : positions.length ? "" : "Henüz pozisyon eklenmedi. Yeni bir pozisyon açabilirsiniz.";
        } catch (error) {
            notice.textContent = error.message;
        }
    }
    byId("newPositionButton").addEventListener("click",()=>{
        positionForm.reset();
        options(positionTemplate,Object.entries(templates).map(([id,value])=>({id,name:value.name})),"Firmaya özel pozisyon");
        positionTemplate.value = "";
        positionTemplate.hidden = false;
        api("company-settings").then(company=>{
            const option = positionTemplate.querySelector('[value="warehouse"]');
            if (option && company.sector?.toLocaleLowerCase("tr").includes("lojistik")) {
                option.textContent = "Depo Personeli · sektör önerisi";
            }
        }).catch(()=>{});
        byId("positionEditor").hidden = false;
        updateCvMatchSummary();
        positionForm.elements.namedItem("name").focus();
    });
    positionTemplate.addEventListener("change",()=>{
        const selection = templates[positionTemplate.value];
        if (!selection || editingPosition) return;
        for (const [name,value] of Object.entries(selection)) {
            positionForm.elements.namedItem(name).value = value;
        }
        updateCvMatchSummary();
    });
    byId("positionDepartmentLink").addEventListener("click",event=>{
        event.preventDefault();
        byId("settingsMenu").click();
    });

    async function loadPositionMembers(position) {
        const panel = byId("positionMembersPanel");
        try {
            const url = "ats/positions/" + position.id + "/members";
            const {users, members} = await api(url);
            panel.replaceChildren();
            const heading = document.createElement("h3");
            heading.textContent = position.name + " · Personel";
            panel.append(heading);
            for (const user of users) {
                const button = document.createElement("button");
                button.type = "button";
                const allowed = members.includes(user.id);
                button.textContent = user.name + (allowed ? " · Yetkiyi kaldır" : " · Yetki ver");
                button.addEventListener("click", async () => {
                    button.disabled = true;
                    try {
                        await api(url + "/" + user.id + "?enabled=" + (!allowed),"PUT");
                        await loadPositionMembers(position);
                    } catch (error) {
                        byId("positionsMessage").textContent = error.message;
                    } finally {
                        button.disabled = false;
                    }
                });
                panel.append(button);
            }
        } catch (error) {
            byId("positionsMessage").textContent = error.message;
        }
    }

    positionForm.addEventListener("reset", () => {
        queueMicrotask(updateCvMatchSummary);
        editingPosition = "";
        byId("positionEditorTitle").textContent = "Pozisyon aç";
    });
    positionForm.addEventListener("submit", async event => {
        event.preventDefault();
        if (!updateCvMatchSummary()) {
            byId("positionsMessage").textContent = "CV eşleşme kriterlerinin biçimini düzeltin.";
            cvMatchInput.focus();
            return;
        }
        const button = event.submitter;
        button.disabled = true;
        const data = Object.fromEntries(new FormData(positionForm));
        data.openings = Number(data.openings);
        try {
            await api(editingPosition ? "positions/" + editingPosition : "positions", editingPosition ? "PATCH" : "POST", data);
            positionForm.reset();
            byId("positionEditor").hidden = true;
            await loadPositions();
        } catch (error) {
            byId("positionsMessage").textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });
    candidateForm.addEventListener("submit", async event => {
        event.preventDefault();
        event.submitter.disabled = true;
        try {
            await api("candidates", "POST", Object.fromEntries(new FormData(candidateForm)));
            candidateForm.reset();
            byId("atsCandidateDetails").open = false;
            byId("atsCandidateMessage").textContent = "Aday kaydedildi.";
            await loadCandidates();
        } catch (error) {
            byId("atsCandidateMessage").textContent = error.message;
        } finally {
            event.submitter.disabled = false;
        }
    });
    byId("candidateManagerClose").addEventListener("click", () => byId("candidateManagerDialog").close());
    document.addEventListener("click", async event => {
        const button = event.target.closest("[data-manage-candidate]");
        if (!button || !window.ikSaasAccess.manager) return;
        const candidate = allCandidates.find(row => row.token === button.dataset.manageCandidate);
        if (!candidate) return;
        managingCandidate = candidate.token;
        managerForm.elements.namedItem("evaluation_status").value = candidate.evaluation_status || "new";
        byId("candidateManagerMessage").textContent = "";
        byId("candidateManagerDialog").showModal();
        try {
            const {users} = await api("assignees");
            const select = managerForm.elements.namedItem("assigned_user_id");
            options(select, users, "Atanmadı");
            select.value = candidate.assigned_user_id || "";
        } catch (error) {
            byId("candidateManagerMessage").textContent = error.message;
        }
    });
    managerForm.addEventListener("submit", async event => {
        event.preventDefault();
        const button = event.submitter;
        button.disabled = true;
        const suffix = button.name;
        const evaluation = managerForm.elements.namedItem("evaluation_status").value;
        const assignee = managerForm.elements.namedItem("assigned_user_id").value;
        try {
            await api("candidates/" + encodeURIComponent(managingCandidate) + "/" + suffix, "PATCH",
                      suffix === "assignment" ? {assigned_user_id: Number(assignee) || null} : {evaluation_status:evaluation});
            byId("candidateManagerDialog").close();
            await loadCandidates();
        } catch (error) {
            byId("candidateManagerMessage").textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    async function loadUsage() {
        const notice = byId("usageMessage");
        const rows = byId("usageRows");
        const cards = byId("usageCards");
        rows.replaceChildren();
        cards.replaceChildren();
        try {
            const data = await api("usage");
            for (const [product, plan] of Object.entries(data.plans)) {
                const effective = window.ikSaasAccess[product] ? "Aktif" : plan.status === "Aktif" ? "Süre dışında" : plan.status;
                const row = rows.insertRow();
                for (const value of [product === "ats" ? "ERNASA ATS" : "ERNASA AI İK",
                    plan.package, effective, plan.starts, plan.ends,
                    data.active_users + " / " + (plan.user_quota ?? "Sınırsız"),
                    product === "ai" ? data.monthly_interviews + " / " + (plan.operation_quota ?? "Sınırsız") : "—"]) {
                    row.insertCell().textContent = value;
                }
                const card = document.createElement("article");
                card.className = "saas-usage-card";
                const title = document.createElement("strong");
                title.textContent = (product === "ats" ? "ERNASA ATS" : "ERNASA AI İK") + " · " + effective;
                card.append(title);
                for (const [label,used,limit] of [["Aktif kullanıcı",data.active_users,plan.user_quota],
                    ...(product === "ai" ? [["Aylık mülakat",data.monthly_interviews,plan.operation_quota]] : [])]) {
                    const caption = document.createElement("p");
                    caption.textContent = label + " · " + used + " / " + (limit ?? "Sınırsız");
                    card.append(caption);
                    if (limit > 0) {
                        const meter = document.createElement("meter");
                        meter.min = 0; meter.max = limit; meter.value = Math.min(used,limit);
                        meter.setAttribute("aria-label",label);
                        card.append(meter);
                    }
                }
                cards.append(card);
            }
            notice.textContent = "";
        } catch (error) {
            notice.textContent = error.message;
        }
    }

    // Görünür sol menü, temel iş akışlarını gösterir. Önceki ekran linkleri
    // erişim/yetki kontrolleriyle birlikte korunur ve içerik sekmelerinden açılır.
    const navigation = byId("portalSidebar");
    const tabs = byId("portalSectionTabs");
    const screenLink = name => navigation.querySelector('[data-screen="' + name + '"]');
    const sectionGroups = {
        dashboard: ["dashboard"],
        flow: ["suite_flow"],
        pool: ["suite_pool","candidates","suite_kanban","cv"],
        positions: ["positions","suite_jobs"],
        interviews: ["interviews","suite_calendar"],
        evaluations: ["suite_evaluations","suite_reports","suite_offers"],
        training: ["training"],
        training_tracking: ["training_tracking"],
        settings: ["suite_settings","personnel","usage","suite_history"]
    };
    const mainForScreen = new Map(Object.entries(sectionGroups).flatMap(([section,screens]) =>
        screens.map(screen => [screen,section])));
    const labelOverrides = {
        "suite_flow":"ERNASA HR FLOW","suite_pool":"Aday Havuzu","suite_jobs":"İş İlanları","suite_calendar":"Görüşme Takvimi",
        "suite_evaluations":"Değerlendirmeler","suite_kanban":"İşe Alım Panosu",
        "suite_reports":"AI Değerlendirme Raporları","suite_offers":"Teklifler",
        "suite_settings":"Firma Ayarları","suite_history":"İşlem Geçmişi",
        "cv":"CV Analizi","candidates":"Aday Takip Merkezi","personnel":"Personel Yönetimi",
        "usage":"Paket ve Kullanım","positions":"Pozisyonlar ve İlanlar",
        "interviews":"Mülakatlar","dashboard":"Ana Sayfa"
    };
    const availableScreens = new Set();
    const activeLinks = new Map();

    function displaySectionTabs(screen) {
        const main = mainForScreen.get(screen);
        const options = sectionGroups[main] || [];
        tabs.replaceChildren();
        for (const name of options) {
            if (!availableScreens.has(name)) continue;
            const link = activeLinks.get(name);
            if (!link) continue;
            const button = document.createElement("button");
            button.type = "button";
            button.className = "portal-subnav-button";
            button.textContent = labelOverrides[name] || link.textContent.trim();
            button.setAttribute("aria-current",name === screen ? "page" : "false");
            button.addEventListener("click",()=>link.click());
            tabs.append(button);
        }
        // Sekme her bölümde zorunlu değildir: yalnızca ikinci bir işlem varsa göster.
        tabs.hidden = tabs.childElementCount < 2 || !main || main === "dashboard";
        for (const [name,link] of activeLinks) {
            if (!link.classList.contains("portal-main-link")) continue;
            link.classList.toggle("active",link.dataset.panelMain === mainForScreen.get(screen));
        }
    }
    function showSidebarUnavailable() {
        // Never expose legacy navigation, including hidden module routes, if
        // the authenticated company metadata cannot be loaded.
        navigation.replaceChildren();
        const status = document.createElement("p");
        status.textContent = "Menü yüklenemedi. Oturumunuzu kontrol edip yeniden deneyin.";
        status.setAttribute("role","status");
        const retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = "Yeniden dene";
        retry.addEventListener("click", () => location.reload());
        navigation.append(status,retry);
    }
    portalMetadataPromise.then(metadata => {
        if (!metadata || !Array.isArray(metadata.products)) {
            showSidebarUnavailable();
            return;
        }
        const ats = metadata.products.includes("ats");
        const ai = metadata.products.includes("ai");
        const manager = ["supervisor","hr_manager"].includes(metadata.role);
        window.ikSaasAccess = {ats, ai, manager, report: ai && manager};
        const selinQueue=byId("selinIntakeMenu");
        if (selinQueue) selinQueue.style.display=ats && ai && manager ? "" : "none";
        const candidates = byId("portalCandidatesLink");
        const home = byId("portalHomeLink");
        const links = {
            dashboard:byId("portalOverviewLink"),
            suite_flow:selinQueue,
            positions:byId("positionsMenu"),
            suite_jobs:byId("jobsMenu"),
            suite_pool:byId("poolMenu"),
            suite_kanban:byId("kanbanMenu"),
            suite_calendar:byId("calendarMenu"),
            suite_evaluations:byId("evaluationsMenu"),
            suite_offers:byId("offersMenu"),
            suite_reports:byId("reportsMenu"),
            suite_settings:byId("settingsMenu"),
            suite_history:byId("historyMenu"),
            training:byId("trainingAssignmentMenu"),
            training_tracking:byId("trainingTrackingMenu"),
            candidates,
            cv:screenLink("cv"),
            interviews:screenLink("interviews"),
            personnel:byId("personnelMenu"),
            usage:byId("usageMenu")
        };
        if (ats && ai && manager) availableScreens.add("suite_flow");
        if (ats) ["positions","suite_jobs","suite_pool","suite_kanban","suite_calendar",
            "suite_evaluations","suite_offers"].forEach(name=>availableScreens.add(name));
        if (ai) ["cv","candidates","interviews","suite_reports"].forEach(name=>availableScreens.add(name));
        if (manager) ["personnel","usage","suite_settings","suite_history"].forEach(name=>availableScreens.add(name));
        // Firma yetkilisi rolü eğitim atama ve takip ekranlarını görür.
        // API erişimi de ayrıca sunucu tarafında denetlenir.
        if (["supervisor","hr_manager","hr_staff"].includes(metadata.role)) {
            availableScreens.add("training");
            availableScreens.add("training_tracking");
        }
        if (!metadata.can_manage_personnel) availableScreens.delete("personnel");
        if (!manager) ["suite_jobs","suite_offers","suite_reports"].forEach(name=>availableScreens.delete(name));
        availableScreens.add("dashboard");
        for (const [name,link] of Object.entries(links)) {
            if (!link || !availableScreens.has(name)) continue;
            activeLinks.set(name,link);
        }
        navigation.replaceChildren();
        // Eski linkler DOM'da kalır: diğer modüllerin .click() çağrıları bozulmaz.
        const hiddenLinks = document.createElement("div");
        hiddenLinks.hidden = true;
        hiddenLinks.id = "portalSecondaryRoutes";
        hiddenLinks.setAttribute("aria-hidden","true");
        const primaries = [
            ["dashboard","dashboard","Ana Sayfa"],
            ["flow","suite_flow","ERNASA HR FLOW"],
            ["pool",ats?"suite_pool":"candidates","Aday Havuzu"],
            ["positions","positions","Pozisyonlar ve İlanlar"],
            ["interviews",ai?"interviews":"suite_calendar","Mülakatlar"],
            ["evaluations","suite_evaluations","Değerlendirmeler"],
            ["training","training","Eğitim Ata"],
            ["training_tracking","training_tracking","Eğitim Takip"],
            ["settings","suite_settings","Ayarlar"]
        ];
        for (const [group,name,label] of primaries) {
            if (!availableScreens.has(name)) continue;
            const link = links[name];
            if (!link) continue;
            link.style.removeProperty("display");
            if (name === "candidates" && link.querySelector("#portalCandidatesLabel")) {
                link.querySelector("#portalCandidatesLabel").textContent = label;
                link.querySelectorAll("i,svg").forEach(icon => icon.remove());
            } else {
                link.textContent = label;
            }
            const icons = {
                dashboard:"house",flow:"bot",pool:"users-round",positions:"briefcase",
                interviews:"messages-square",evaluations:"clipboard-check",
                training:"graduation-cap",training_tracking:"chart-no-axes-column",settings:"settings"
            };
            const symbol = document.createElement("i");
            symbol.setAttribute("data-lucide",icons[group]);
            link.prepend(symbol);
            link.classList.add("portal-main-link");
            link.dataset.panelMain = group;
            navigation.append(link);
        }
        for (const [name,link] of Object.entries(links)) {
            if (!link || link.parentNode === navigation) continue;
            if (availableScreens.has(name)) {
                link.style.removeProperty("display");
                link.dataset.panelMain = mainForScreen.get(name) || "dashboard";
                hiddenLinks.append(link);
            }
        }
        // Ana Sayfa ve Genel Bakış aynı dashboard ekranıdır: tek görünür
        // Ana Sayfa bağlantısı kullan; eski kök linki gizli tut.
        home.hidden = true;
        hiddenLinks.append(home);
        navigation.append(hiddenLinks);
        if (ats) {
            byId("atsCandidateDetails").style.removeProperty("display");
            byId("newPositionButton").hidden = !manager;
            loadPositions();
        }
        if (ai && !candidates.classList.contains("portal-main-link")) {
            candidates.querySelector("#portalCandidatesLabel")?.replaceChildren(
                document.createTextNode("Aday Takip Merkezi"));
        }
        byId("portalProfileActions").hidden = false;
        renderCandidates();
        displaySectionTabs("dashboard");
        if (window.lucide?.createIcons) window.lucide.createIcons();
        window.ernasaRenderPortalIcons?.(navigation);
    }).catch(error => {
        console.error("Firma menüsü oluşturulamadı:",error);
        showSidebarUnavailable();
    }).finally(() => {
        // Reveal only the complete role-scoped menu, never the original HTML.
        navigation.style.removeProperty("visibility");
        navigation.removeAttribute("aria-busy");
    });
    document.addEventListener("ik-portal-screen", event => {
        if (event.detail === "positions" && window.ikSaasAccess.ats) loadPositions();
        if (event.detail === "usage" && window.ikSaasAccess.manager) loadUsage();
        displaySectionTabs(event.detail);
    });

    const profileDialog = byId("profileDialog");
    const passwordForm = byId("ownPasswordForm");
    async function showProfile(change) {
        byId("profileDialogTitle").textContent = change ? "Parola Değiştir" : "Profilim";
        passwordForm.reset();
        passwordForm.hidden = !change;
        byId("profileIdentity").textContent = "";
        byId("profileMessage").textContent = "";
        profileDialog.showModal();
        try {
            const values = await api("profile");
            const roles = {supervisor:"Süpervizör",hr_manager:"İK Müdürü",hr_staff:"İK Personeli"};
            byId("profileIdentity").textContent = [values.name,values.email,roles[values.role]].join(" · ");
        } catch (error) {
            byId("profileMessage").textContent = error.message;
        }
    }
    byId("profileOpen").addEventListener("click",()=>showProfile(false));
    byId("passwordOpen").addEventListener("click",()=>showProfile(true));
    profileDialog.addEventListener("close",()=>passwordForm.reset());
    window.addEventListener("pagehide",()=>passwordForm.reset());
    passwordForm.addEventListener("submit",async event=>{
        event.preventDefault();
        const button = event.submitter;
        button.disabled = true;
        try {
            await api("profile/password","PUT",Object.fromEntries(new FormData(passwordForm)));
            passwordForm.reset();
            location.replace("/ik-giris");
        } catch (error) {
            byId("profileMessage").textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });
}
