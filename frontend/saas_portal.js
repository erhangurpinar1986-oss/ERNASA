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
            options(byId("portalPosition"), open, "Açık pozisyon seçin");
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
        positionForm.elements.namedItem("name").focus();
    });
    positionTemplate.addEventListener("change",()=>{
        const selection = templates[positionTemplate.value];
        if (!selection || editingPosition) return;
        for (const [name,value] of Object.entries(selection)) {
            positionForm.elements.namedItem(name).value = value;
        }
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

    byId("portalPosition").addEventListener("change", event => {
        const selection = event.target;
        byId("positionName").value = selection.value ? selection.selectedOptions[0].text : "";
    });
    positionForm.addEventListener("reset", () => {
        editingPosition = "";
        byId("positionEditorTitle").textContent = "Pozisyon aç";
    });
    positionForm.addEventListener("submit", async event => {
        event.preventDefault();
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

    function section(title, items) {
        const navigation = byId("portalSidebar");
        const label = document.createElement("div");
        label.className = "saas-nav-label";
        label.textContent = title;
        const links = items.filter(Boolean);
        links.forEach(link => link.style.removeProperty("display"));
        navigation.append(label, ...links);
    }
    const navigation = byId("portalSidebar");
    const screenLink = name => navigation.querySelector('[data-screen="' + name + '"]');
    navigation.querySelectorAll('[data-product="ai"]').forEach(link => { link.style.display = "none"; });
    portalMetadataPromise.then(metadata => {
        if (!metadata) return;
        const ats = metadata.products.includes("ats");
        const ai = metadata.products.includes("ai");
        const manager = ["supervisor","hr_manager"].includes(metadata.role);
        window.ikSaasAccess = {ats, ai, manager, report: ai && manager};
        const home = byId("portalHomeLink"), overview = byId("portalOverviewLink");
        const positions = byId("positionsMenu"), candidates = byId("portalCandidatesLink");
        const cv = screenLink("cv"), interviews = screenLink("interviews");
        const personnel = byId("personnelMenu"), usage = byId("usageMenu");
        const jobs = byId("jobsMenu"), pool = byId("poolMenu"), board = byId("kanbanMenu");
        const calendar = byId("calendarMenu"), evaluations = byId("evaluationsMenu"), offers = byId("offersMenu");
        const reports = byId("reportsMenu"), settings = byId("settingsMenu"), history = byId("historyMenu");
        navigation.replaceChildren();
        section("GENEL", [home, overview]);
        if (ats) {
            positions.style.removeProperty("display");
            section("ERNASA ATS", [positions, manager && jobs, pool, board, calendar, evaluations, manager && offers]);
            byId("atsCandidateDetails").style.removeProperty("display");
            byId("newPositionButton").hidden = !manager;
            if (ai) {
                byId("positionName").style.display = "none";
                byId("portalPosition").style.removeProperty("display");
            }
            loadPositions();
        }
        if (ai) {
            cv.style.removeProperty("display");
            interviews.style.removeProperty("display");
            candidates.querySelector("#portalCandidatesLabel").textContent = "Aday Takip Merkezi";
            section("ERNASA AI İK", [cv, candidates, interviews, manager && reports]);
        }
        if (manager) {
            usage.style.removeProperty("display");
            section("FİRMA YÖNETİMİ", [metadata.can_manage_personnel ? personnel : null, usage, settings, history]);
        }
        byId("portalProfileActions").hidden = false;
        renderCandidates();
    });
    document.addEventListener("ik-portal-screen", event => {
        if (event.detail === "positions" && window.ikSaasAccess.ats) loadPositions();
        if (event.detail === "usage" && window.ikSaasAccess.manager) loadUsage();
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
