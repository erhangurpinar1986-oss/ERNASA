"use strict";

if (companyPortalPage) {
    const suite = document.getElementById("atsSuiteScreen");
    const stageLabels = {
        new:"Yeni Başvuru",screening:"Ön Eleme",shortlist:"Kısa Liste",interview:"Görüşme",
        evaluation:"Değerlendirme",offer:"Teklif",hired:"İşe Alındı"
    };
    const closureLabels = {negative:"Olumsuz",withdrawn:"Aday çekildi",position_closed:"Pozisyon kapandı"};
    const offerLabels = {draft:"Taslak",approved:"Onaylandı",ready:"Gönderime hazır",sent:"Gönderildi",
        accepted:"Kabul edildi",rejected:"Reddedildi",withdrawn:"Geri çekildi"};
    const nextOffers = {draft:["approved","withdrawn"],approved:["ready","withdrawn"],ready:["sent","withdrawn"],
        sent:["accepted","rejected","withdrawn"]};
    const scheduleLabels = {planned:"Planlı",completed:"Tamamlandı",cancelled:"İptal"};
    const interviewLabels = {waiting:"Bekliyor",created:"Oluşturuldu",opened:"Açıldı",
        started:"Devam ediyor",completed:"Tamamlandı",expired:"Süresi doldu"};
    let metadata, generation = 0, chosenApplication = "", chosenPosition = "";
    const manager = () => ["supervisor","hr_manager"].includes(metadata.role);
    const apiRoot = "/api/ik/";
    const appRoute = id => "ats/applications/" + encodeURIComponent(id);

    function el(tag, text = "", attributes = {}) {
        const item = document.createElement(tag);
        item.textContent = text;
        for (const [key, value] of Object.entries(attributes)) item.setAttribute(key, value);
        return item;
    }
    function action(label, run) {
        const button = el("button", label, {type:"button"});
        button.addEventListener("click", () => run(button));
        return button;
    }
    function link(label, path) {
        return el("a",label,{href:path,target:"_blank",rel:"noopener noreferrer"});
    }
    function select(name, choices, current = "") {
        const input = el("select","",{name});
        for (const [value,label] of choices) input.add(new Option(label,value));
        if (current !== undefined) input.value = String(current);
        return input;
    }
    function input(name, type = "text", value = "", attributes = {}) {
        const field = el("input","",{name,type,...attributes});
        field.value = value ?? "";
        return field;
    }
    function textarea(name, value = "", max = 6000) {
        const field = el("textarea","",{name,maxlength:max});
        field.value = value ?? "";
        return field;
    }
    function field(label, control) {
        const value = el("label",label);
        value.append(control);
        return value;
    }
    function form(items, label, submit) {
        const result = el("form","",{class:"saas-form"});
        const button = el("button", label, {type:"submit"});
        result.append(...items,button);
        result.addEventListener("submit", async event => {
            event.preventDefault();
            button.disabled = true;
            try { await submit(Object.fromEntries(new FormData(result)),result); }
            catch (error) { notice(error.message,true); }
            finally { button.disabled = false; }
        });
        return result;
    }
    function notice(value, error = false) {
        const status = suite.querySelector("[role=status]");
        if (status) {
            status.textContent = value;
            status.classList.toggle("saas-error",error);
        }
    }
    async function api(path, method = "GET", data) {
        const spec = {method,cache:"no-store"};
        if (data instanceof FormData) spec.body = data;
        else if (data !== undefined) {
            spec.headers = {"Content-Type":"application/json"};
            spec.body = JSON.stringify(data);
        }
        return portalJson(apiRoot + path,spec,"İşlem tamamlanamadı. Alanları kontrol edip tekrar deneyin.");
    }
    function table(headers, rows) {
        if (!rows.length) return el("p","Bu listede henüz kayıt yok.",{class:"v5-empty"});
        const wrap = el("div","",{class:"saas-table-wrap"});
        const grid = el("table","",{class:"saas-table"});
        grid.createTHead().insertRow().append(...headers.map(label => el("th",label,{scope:"col"})));
        const body = grid.createTBody();
        for (const values of rows) {
            const row = body.insertRow();
            for (const value of values) {
                const cell = row.insertCell();
                if (value instanceof Node) cell.append(value);
                else cell.textContent = value == null || value === "" ? "—" : value;
            }
        }
        wrap.append(grid);
        return wrap;
    }
    function day(value, zone = "Europe/Istanbul") {
        return value ? new Date(value).toLocaleString("tr-TR",{timeZone:zone}) : "—";
    }
    function dateInput(value, zone) {
        if (!value) return "";
        const parts = new Intl.DateTimeFormat("sv-SE",{timeZone:zone,dateStyle:"short",timeStyle:"short"}).format(new Date(value));
        return parts.replace(" ","T");
    }
    async function values(positionId = "", query = "", stage = "", offset = 0, assignee = "") {
        return api("ats/applications?" + new URLSearchParams({position_id:positionId,search:query,stage,limit:"25",offset,assignee}));
    }
    async function positions(openOnly = false) {
        const data = await api("positions");
        return data.positions.filter(row => !openOnly || row.open).map(row => [row.id,row.name]);
    }
    async function applicationPicker(container, onPick) {
        const result = await values("", "", "", 0);
        const picker = select("application", [["","Başvuru seçin"],...result.applications.map(row =>
            [row.id,row.name + " · " + row.position])],chosenApplication);
        if (chosenApplication && !result.applications.some(row => row.id === chosenApplication)) {
            try {
                const {application} = await api(appRoute(chosenApplication));
                picker.add(new Option(application.name + " · " + application.position,application.id));
                picker.value = application.id;
            } catch (error) { notice(error.message); }
        }
        const detail = el("div");
        const selection = el("div","",{class:"saas-form saas-application-picker"});
        selection.append(field("Başvuru",picker));
        container.append(selection,detail);
        const show = async () => {
            detail.replaceChildren();
            if (!picker.value) return;
            chosenApplication = picker.value;
            try { await onPick(picker.value,detail); } catch (error) { notice(error.message); }
        };
        picker.addEventListener("change",show);
        if (picker.value) await show();
    }

    async function jobs(root) {
        const [data, choices] = await Promise.all([api("ats/jobs"),positions(true)]);
        const which = select("position_id",[["","Pozisyon seçin"],...choices]);
        const title = input("title","text","",{required:"",maxlength:160});
        const summary = textarea("summary");
        const qualifications = textarea("qualifications");
        const conditions = textarea("work_conditions");
        const policy = textarea("notice");
        const version = input("notice_version","text","",{maxlength:40});
        const state = select("state",[["draft","Taslak"],["published","Yerel yayına al"],["closed","Kapat"]],"draft");
        const confirm = input("confirm_publish","checkbox","1");
        which.required = summary.required = policy.required = version.required = true;
        const loadJob = () => {
            const job = data.jobs.find(row => String(row.position_id) === which.value);
            title.value = job?.title || "";
            summary.value = job?.summary || "";
            qualifications.value = job?.qualifications || "";
            conditions.value = job?.work_conditions || "";
            policy.value = job?.notice || "";
            version.value = job?.notice_version || "";
            state.value = job?.state || "draft";
            confirm.checked = false;
            preview.hidden = true;
        };
        const preview = el("section","",{class:"saas-inline-preview",hidden:""});
        which.addEventListener("change",loadJob);
        root.append(table(["İlan","Durum","Aday bağlantısı","İşlem"],data.jobs.map(row => {
            const actions = el("div","",{class:"saas-actions"});
            if (choices.some(([id])=>String(id)===String(row.position_id))) actions.append(action("Düzenle",()=>{
                which.value = String(row.position_id);
                loadJob();
                title.focus();
            }));
            actions.append(action("Başvuruları görüntüle",()=>{
                const menu = document.getElementById("poolMenu");
                menu.dataset.filterPosition = row.position_id;
                menu.click();
            }));
            return [row.title,({draft:"Taslak",published:"Yayında",closed:"Kapalı"})[row.state],
                row.state === "published" ? link("Yerel başvuru bağlantısını aç","/basvuru/" + row.id) : "—",actions];
        })));
        if (!choices.length) {
            root.append(action("Yeni Pozisyon Oluştur",()=>{
                document.getElementById("positionsMenu").click();
                document.getElementById("newPositionButton").click();
            }));
            return;
        }
        const previewButton = action("İlanı önizle",()=>{
            preview.replaceChildren(el("h3",title.value || "İlan başlığı"),el("p",summary.value),
                el("h4","Aranan nitelikler"),el("p",qualifications.value || "—"),
                el("h4","Çalışma koşulları"),el("p",conditions.value || "—"));
            preview.hidden = false;
        });
        root.append(el("p","Yayın, yalnızca ERNASA’nın yerel başvuru bağlantısını açar. Dış iş ilanı platformlarına gönderim yapılmaz."),
            form([field("Pozisyon",which),field("İlan başlığı",title),field("İş tanımı",summary),
            field("Aranan nitelikler",qualifications),field("Çalışma koşulları",conditions),
            field("Aday aydınlatma metni",policy),field("Metin sürümü",version),field("Durum",state),
            field("Yerel yayını onaylıyorum",confirm)],"İlanı kaydet",async data => {
            await api("ats/jobs","POST",{...data,confirm_publish:data.confirm_publish==="1"});
            await open("jobs");
            notice("İlan kaydedildi.");
        }),previewButton,preview);
    }

    function cvImport(root, choices, openPositions, contextPosition, refresh, manualApplication) {
        const limit = 20, maxBytes = 10 * 1024 * 1024;
        const fileInput = input("cv","file","",{accept:".pdf,.docx",multiple:"",required:""});
        const preview = el("div");
        const progress = el("p","",{role:"status"});
        const positionChoices = [["","Pozisyon seçin"],...openPositions];
        const selectionLabels = {name:"Ad soyad",email:"E-posta",phone:"Telefon",position_id:"Pozisyon"};
        root.append(el("h3","CV Yükle"),form([
            field("PDF veya DOCX (en fazla 20, dosya başına 10 MB)",fileInput)
        ],"Önizle",async () => {
            const files = [...fileInput.files];
            if (files.length > limit) throw new Error("Bir seferde en fazla 20 dosya seçin.");
            fileInput.disabled = true;
            preview.replaceChildren();
            const rows = [];
            try {
                for (const file of files) {
                    if (!root.isConnected) return;
                    progress.textContent = "CV okunuyor · " + (rows.length + 1) + " / " + files.length;
                    try {
                        if (file.size > maxBytes) throw new Error("Dosya 10 MB sınırını aşıyor.");
                        const data = new FormData();
                        data.append("cv",file);
                        rows.push({file,data:await api("ats/cv-import/preview","POST",data)});
                    } catch (error) {
                        rows.push({file,error:error.message});
                    }
                }
                progress.textContent = "";
                renderPreview(rows);
            } finally { fileInput.disabled = false; }
        }),progress,preview);

        function renderPreview(rows) {
            const panel = el("fieldset","",{class:"ats-cv-preview","aria-label":"CV önizlemesi"});
            const summary = el("p","",{role:"status"});
            const save = action("Seçilenleri kaydet",saveRows);
            const ready = rows.filter(row => row.data);
            const contextName = choices.find(([id]) => String(id) === contextPosition)?.[1];
            const bulk = select("bulk_position",positionChoices);
            const refreshSave = () => {
                save.disabled = !ready.some(row => row.checked.checked && !row.done);
            };
            const checkFields = row => {
                const missing = Object.entries(row.fields).filter(([,control]) => !control.value.trim())
                    .map(([key]) => selectionLabels[key]);
                row.status.textContent = missing.length ? "Eksik · " + missing.join(", ") : "Kayda hazır";
            };
            const grid = table(["Seç","Belge","Ad soyad","E-posta","Telefon","Pozisyon","Durum"],
                rows.map(row => {
                    row.status = el("span");
                    if (!row.data) {
                        row.status.textContent = "Atlandı · " + row.error;
                        if (manualApplication && /^CV (okunamadı|içinde okunabilir metin)/.test(row.error)) {
                            row.status.append(el("p","Manuel başvuruyu oluşturup CV’yi aday detayından ekleyebilirsiniz."),
                                action("Manuel başvuru oluştur",manualApplication));
                        }
                        return ["",row.file.name,"","","","",row.status];
                    }
                    row.checked = input("selected","checkbox","1",{"aria-label":row.file.name + " belgesini seç"});
                    row.checked.checked = true;
                    row.checked.addEventListener("change",refreshSave);
                    const scopedChoices = contextPosition ? [[contextPosition,contextName || contextPosition]] : positionChoices;
                    row.fields = {
                        name:input("name","text",row.data.name,{maxlength:160}),
                        email:input("email","email",row.data.email,{maxlength:254}),
                        phone:input("phone","tel",row.data.phone,{maxlength:50}),
                        position_id:select("position_id",scopedChoices,contextPosition)
                    };
                    row.fields.position_id.disabled = Boolean(contextPosition);
                    for (const [key,control] of Object.entries(row.fields)) {
                        control.setAttribute("aria-label",selectionLabels[key] + " · " + row.file.name);
                        control.addEventListener("input",() => checkFields(row));
                    }
                    checkFields(row);
                    return [row.checked,row.file.name,...Object.values(row.fields),row.status];
                }));
            if (ready.length && !contextPosition) {
                panel.append(field("Tüm CV’ler için pozisyon",bulk));
                bulk.addEventListener("change",() => {
                    for (const row of ready.filter(row => !row.done)) {
                        row.fields.position_id.value = bulk.value;
                        checkFields(row);
                    }
                });
            }
            if (contextName) panel.append(el("p","Pozisyon · " + contextName));
            panel.append(grid,save);
            preview.replaceChildren(panel,summary);
            refreshSave();

            async function saveRows() {
                const selected = ready.filter(row => row.checked.checked && !row.done);
                panel.disabled = fileInput.disabled = true;
                try {
                    for (const [index,row] of selected.entries()) {
                        if (!root.isConnected) return;
                        summary.textContent = "CV kaydediliyor · " + (index + 1) + " / " + selected.length;
                        const selection = Object.fromEntries(Object.entries(row.fields)
                            .map(([key,control]) => [key,control.value.trim()]));
                        try {
                            const body = new FormData();
                            body.append("cv",row.file);
                            body.append("selection",JSON.stringify({...selection,import_id:row.data.import_id}));
                            const result = await api("ats/cv-import/save","POST",body);
                            row.outcome = result.status;
                            row.status.textContent = result.status === "saved" ? "Kaydedildi" : "Atlandı · " + result.message;
                            row.done = true;
                            row.checked.checked = false;
                            row.checked.disabled = true;
                            Object.values(row.fields).forEach(control => control.disabled = true);
                        } catch (error) {
                            row.outcome = "correction";
                            row.status.textContent = "Düzeltme veya yeniden deneme bekliyor · " + error.message;
                        }
                    }
                    const counts = {saved:0,skipped:0,correction:0,unselected:0};
                    for (const row of rows) counts[row.outcome || (row.error ? "skipped" : "unselected")]++;
                    summary.textContent = counts.saved + " kaydedildi · " + counts.skipped + " atlandı · " +
                        counts.correction + " düzeltme veya yeniden deneme bekliyor · " + counts.unselected + " seçilmedi";
                    if (counts.saved) await refresh();
                } finally {
                    panel.disabled = fileInput.disabled = false;
                    refreshSave();
                }
            }
        }
        return fileInput;
    }

    async function pool(root) {
        const choices = await positions();
        const search = input("search","search","",{maxlength:160});
        const poolMenu = document.getElementById("poolMenu");
        const openApplication = poolMenu.dataset.openApplication;
        delete poolMenu.dataset.openApplication;
        const contextPosition = poolMenu.dataset.filterPosition || "";
        const target = select("position_id",[["","Tüm pozisyonlar"],...choices],contextPosition);
        delete poolMenu.dataset.filterPosition;
        const stage = select("stage",[["","Tüm aşamalar"],...Object.entries(stageLabels)]);
        const people = manager() ? (await api("assignees")).users : [{id:metadata.user_id,name:metadata.user_name}];
        const assignee = select("assignee",[["","Tüm İK sorumluları"],["unassigned","Atanmamış"],...people.map(user=>[user.id,user.name])]);
        const results = el("section","",{class:"saas-panel",tabindex:"-1"});
        const details = el("section","",{class:"saas-panel",tabindex:"-1",hidden:""});
        const editor = el("section","",{class:"saas-panel",hidden:""});
        const importer = el("section","",{class:"saas-panel",hidden:""});
        const reveal = () => { editor.hidden = false; editor.querySelector("[name=name]")?.focus(); };
        const revealDetails = async id => {
            details.hidden = false;
            await showApplication(details,id,draw);
            details.focus();
        };
        let offset = 0, request = 0, visible = [];
        const draw = async () => {
            const current = ++request;
            try {
                const data = await values(target.value,search.value,stage.value,offset,assignee.value);
                if (current !== request) return;
                visible = data.applications;
                results.replaceChildren(el("h3","Başvurular"),el("p",data.total + " başvuru"),data.total === 0
                    ? el("p","Henüz aday bulunmuyor.",{class:"v5-empty"}) : table(["Ad soyad","Pozisyon","Kaynak","Aşama","İşlem"],
                    data.applications.map(row => [row.name,row.position,row.source,closureLabels[row.closure] || stageLabels[row.stage],
                        action("Dosyayı aç",async () => {
                            chosenApplication = row.id;
                            await revealDetails(row.id);
                        })])));
                if (offset > 0) results.append(action("Önceki",() => {offset -= 25;draw();}));
                if (offset + 25 < data.total) results.append(action("Sonraki",() => {offset += 25;draw();}));
            } catch (error) { notice(error.message); }
        };
        const openPositions = await positions(true);
        const cvInput = cvImport(importer,choices,openPositions,contextPosition,
            async () => {offset=0;await draw();},openPositions.length ? reveal : null);
        const actions = el("div","",{class:"saas-actions"});
        if (openPositions.length) actions.append(action("Yeni Aday Ekle",reveal));
        actions.append(action("Başvuruları Listele",async()=>{offset=0;await draw();results.focus();}),action("CV Yükle",()=>{
            importer.hidden = false;
            cvInput.focus();
        }));
        root.append(actions,form([field("Ad veya e-posta",search),field("Pozisyon",target),field("Aşama",stage),
                                  field("İK sorumlusu",assignee)],
                         "Filtrele",() => {offset=0;return draw();}),importer,results,editor,details);
        if (openPositions.length) editor.append(el("h3","Manuel başvuru"),form([
            field("Ad soyad",input("name","text","",{required:"",maxlength:160})),
            field("E-posta",input("email","email","",{required:"",maxlength:254})),
            field("Telefon",input("phone","tel","",{maxlength:50})),
            field("Pozisyon",select("position_id",openPositions,openPositions[0][0])),
            field("Kaynak",input("source","text","Manuel")),field("Etiketler",input("tags"))
        ],"Başvuruyu kaydet",async (body,form) => {
            const saved = await api("ats/applications","POST",body);
            form.reset();
            editor.hidden = true;
            notice(saved.existing_candidate ? "Başvuru mevcut aday profiline eklendi." : "Başvuru kaydedildi.");
            await draw();
        }));
        await draw();
        if (openApplication && visible.some(row=>row.id===openApplication)) await revealDetails(openApplication);
    }

    async function showApplication(root,id,onNameChanged) {
        const reload = () => showApplication(root,id,onNameChanged);
        root.replaceChildren();
        try {
            const data = await api(appRoute(id));
            const row = data.application;
            root.append(el("h3",row.name+" · "+row.position),el("p",(row.email || "—")+" · "+(row.phone || "—")),
                        el("p",stageLabels[row.stage]+" · "+(row.tags || "Etiket yok")));
            if (manager()) {
                const name = input("name","text",row.name,{required:"",maxlength:160});
                const editName = form([field("Ad soyad",name)],"Kaydet",async fields => {
                    await api(appRoute(id)+"/candidate-name","PATCH",fields);
                    await Promise.all([reload(),onNameChanged()]);
                    notice("Adayın ad soyadı güncellendi.");
                });
                editName.hidden = true;
                const editButton = action("Ad soyadı düzenle",button => {
                    editName.hidden = !editName.hidden;
                    button.setAttribute("aria-expanded",String(!editName.hidden));
                    if (!editName.hidden) name.focus();
                });
                editButton.setAttribute("aria-expanded","false");
                root.append(editButton,editName);
                const {users} = await api("assignees");
                const assigned = select("assignee",[["","Atanmadı"],...users.map(user => [user.id,user.name])],row.assigned_user_id || "");
                root.append(form([field("İK sorumlusu",assigned)],"Ata",async data => {
                    await api(appRoute(id)+"/assignment","PATCH",{assigned_user_id:Number(data.assignee) || null});
                    await reload();
                    notice("İK sorumlusu güncellendi.");
                }));
            }
            root.append(el("h4","Notlar"),...data.notes.map(item => el("p",day(item.created_at)+" · "+item.note)));
            root.append(form([field("Aday notu",textarea("note","",4000))],"Not ekle",async body => {
                await api(appRoute(id)+"/notes","POST",body);
                await reload();
                notice("Aday notu kaydedildi.");
            }));
            root.append(el("h4","Belgeler"));
            for (const doc of data.documents) {
                const path = appRoute(id)+"/documents/"+doc.id;
                const item = el("p");
                item.append(link(doc.filename,apiRoot+path));
                if (metadata.products.includes("ats")) {
                    item.append(action("CV eşleşmesini incele",async () => {
                        try {
                            const matching = await api(path+"/match");
                            const evidence = document.createElement("div");
                            evidence.append(el("p", matching.match_score === null ? matching.reason :
                                "CV–pozisyon eşleşme puanı · %" + matching.match_score));
                            if (matching.evidence_coverage !== null) {
                                evidence.append(el("p","CV kanıt kapsamı · %" + matching.evidence_coverage));
                            }
                            evidence.append(table(["Kategori","İş gereksinimi","Ağırlık","Durum","CV dayanağı"],
                                matching.requirements.map(row => [row.category,row.requirement,row.weight,row.status,
                                    row.evidence_items.map(item=>item.text+(item.page ? " (s. "+item.page+")" : "")).join(" · ")])));
                            evidence.append(el("p","Puan İK incelemesi içindir. Eksik kanıt puana dahil edilmez."));
                            item.nextElementSibling?.classList.contains("ats-evidence") && item.nextElementSibling.remove();
                            evidence.className = "ats-evidence";
                            item.after(evidence);
                        } catch(error) { notice(error.message); }
                    }));
                }
                root.append(item);
            }
            const cv = input("cv","file","",{accept:".pdf,.docx",required:"",multiple:""});
            const uploadStatus = el("p","",{role:"status"});
            const uploadRoute = appRoute(id)+"/upload-jobs";
            async function followUploads(ids, errors = []) {
                while (uploadStatus.isConnected) {
                    const {jobs} = await api(uploadRoute);
                    if (!uploadStatus.isConnected) return;
                    const selected = jobs.filter(job=>ids.has(job.id));
                    const ready = selected.filter(job=>job.status==="ready").length;
                    const pending = selected.filter(job=>job.status==="pending");
                    if (pending.length || selected.length < ids.size) {
                        uploadStatus.textContent = "CV işleniyor · " + ready + " / " + ids.size +
                            (pending.some(job=>job.error) ? " · Geçici hata, yeniden denenecek." : "");
                        await new Promise(resolve=>setTimeout(resolve,500));
                        continue;
                    }
                    errors.push(...selected.filter(job=>job.status==="failed").map(job=>job.filename+" · "+job.error));
                    await reload();
                    if (root.isConnected) notice(ready+" CV yüklendi."+(errors.length ? " " + errors.join(" | ") : ""),errors.length>0);
                    return;
                }
            }
            root.append(form([field("PDF veya DOCX CV (en fazla 20, dosya başına 10 MB)",cv)],"CV yükle",async () => {
                const files = [...cv.files];
                if (files.length > 20) throw new Error("Bir seferde en fazla 20 dosya seçin.");
                const errors = [];
                const ids = new Set();
                for (const [index,file] of files.entries()) {
                    if (!uploadStatus.isConnected) return;
                    uploadStatus.textContent = "CV kuyruğa ekleniyor · " + (index + 1) + " / " + files.length;
                    try {
                        if (file.size > 10 * 1024 * 1024) throw new Error("Dosya 10 MB sınırını aşıyor.");
                        const body = new FormData();
                        body.append("cv",file);
                        const job = await api(uploadRoute,"POST",body);
                        ids.add(job.id);
                    } catch (error) {
                        errors.push(file.name + " · " + error.message);
                    }
                }
                await followUploads(ids,errors);
            }),uploadStatus);
            const {jobs} = await api(uploadRoute);
            const pending = new Set(jobs.filter(job=>job.status==="pending").map(job=>job.id));
            if (pending.size) followUploads(pending).catch(error=>{
                if (uploadStatus.isConnected) uploadStatus.textContent=error.message;
            });
            else if (jobs.some(job=>job.status==="failed")) {
                const failure = jobs.find(job=>job.status==="failed");
                uploadStatus.textContent = failure.filename+" · "+failure.error;
            }
            if (data.interviews) {
                root.append(el("h4","AI mülakatlar"));
                for (const interview of data.interviews) {
                    const item = el("p",(interviewLabels[interview.status] || "Durum bilinmiyor")+" · ");
                    if (interview.status === "completed" && manager()) item.append(link("PDF", "/api/hr/interviews/"+interview.token+"/pdf"));
                    else item.append(link("Aday bağlantısı","/interview/"+interview.token));
                    root.append(item);
                }
                const docs = [["","CV kullanma"],...data.documents.map(doc => [doc.id,doc.filename])];
                root.append(form([el("p","Davet edilecek aday · " + row.name),
                    field("CV",select("document_id",docs))],"AI mülakat bağlantısı oluştur",async fields => {
                    const invite = await api(appRoute(id)+"/ai-interview","POST",{document_id:fields.document_id || null});
                    await reload();
                    const shared = buildInterviewShare(invite);
                    const preview = el("section","",{class:"saas-panel",tabindex:"-1"});
                    const message = textarea("invite_message",shared.message);
                    message.readOnly = true;
                    message.rows = 6;
                    const draft = el("div","",{class:"saas-form"});
                    draft.append(field("Davet mesajı",message));
                    const actions = el("div","",{class:"saas-actions"});
                    actions.append(link("Mülakat bağlantısı",invite.interview_url));
                    if (shared.whatsapp) actions.append(link("WhatsApp",shared.whatsapp));
                    if (invite.email) {
                        actions.append(link("PrivateEmail'de Aç", "https://privateemail.com/spm/mail/INBOX/"));
                        const to = input("draft_to","text",invite.email,{readOnly:""});
                        const subject = input("draft_subject","text",invite.company+" · "+invite.position+" mülakat daveti",{readOnly:""});
                        draft.append(field("Alıcı",to),action("Alıcıyı Kopyala",()=>navigator.clipboard.writeText(to.value)),
                            field("Konu",subject),action("Konuyu Kopyala",()=>navigator.clipboard.writeText(subject.value)),
                            action("Davet Metnini Kopyala",()=>navigator.clipboard.writeText(message.value)));
                    }
                    if (invite.email && invite.interview_id) actions.append(action("Kurumsal E-posta Gönder",async button => {
                        button.disabled = true;
                        try {
                            const result = await api("interviews/" + encodeURIComponent(invite.interview_id) + "/send-email","POST",{});
                            notice(result.message || "Davet kurumsal e-postayla gönderildi.");
                        } catch (error) {
                            notice(error.message,true);
                            button.disabled = false;
                        }
                    }));
                    preview.append(el("h4","Davet oluşturuldu"),draft,actions);
                    root.prepend(preview);
                    preview.focus();
                }));
            }
            if (data.history.length) root.append(el("h4","Aşama geçmişi"),table(["Önceki","Sonraki","Tarih"],
                data.history.map(event=>[stageLabels[event.previous],closureLabels[event.closure] || stageLabels[event.next],day(event.occurred_at)])));
            if (manager()) root.append(link("Başvuru verisini dışa aktar",apiRoot+appRoute(id)+"/export"),
                form([field("Gizlilik talebi",select("kind",[["erase","Silme talebi"],["anonymize","Anonimleştirme"],["export","Dışa aktarma"]],"erase"))],
                     "Talebi kaydet",async fields => {
                        await api(appRoute(id)+"/privacy-requests","POST",fields);
                        notice("Talep inceleme için kaydedildi.");
                     }));
        } catch(error) { notice(error.message); }
    }

    async function kanban(root) {
        const choices = await positions();
        const picker = select("position_id",[["","Pozisyon seçin"],...choices],chosenPosition);
        const output = el("div");
        let offset = 0;
        const draw = async () => {
            output.replaceChildren();
            if (!picker.value) {
                output.append(el("p",choices.length ? "Panoyu görmek için bir pozisyon seçin." : "Pano için önce bir pozisyon oluşturun.",{class:"v5-empty"}));
                return;
            }
            chosenPosition = picker.value;
            try {
                const data = await values(chosenPosition,"","",offset);
                const board = el("div","",{class:"ats-kanban"});
                const transitions = manager() || metadata.permissions.includes("ats_stage");
                const move = async (row,next,closure = null) => {
                    try {
                        await api(appRoute(row.id)+"/stage","PATCH",{stage:next,closure,revision:row.revision});
                        await draw();
                        notice("Başvuru aşaması güncellendi.");
                    } catch(error) { notice(error.message);await draw(); }
                };
                for (const [stage,label] of Object.entries(stageLabels)) {
                    const column = el("section","",{class:"ats-kanban-column"});
                    const cards = data.applications.filter(item=>item.stage===stage && !item.closure);
                    column.append(el("h3",label+" · "+cards.length));
                    if (transitions) {
                        column.addEventListener("dragover",event => event.preventDefault());
                        column.addEventListener("drop",event => {
                            event.preventDefault();
                            const id = event.dataTransfer.getData("text/plain");
                            const row = data.applications.find(item=>item.id===id);
                            if (row) move(row,stage,row.closure);
                        });
                    }
                    for (const row of cards) {
                        const card = el("article","",{class:"ats-kanban-card"});
                        card.append(el("strong",row.name),el("p",row.position),el("small",day(row.created_at)),
                                    el("p",row.assigned_user_id ? "Sorumlu #"+row.assigned_user_id : "Atanmadı"));
                        if (transitions) {
                            card.draggable = true;
                            card.addEventListener("dragstart",event=>event.dataTransfer.setData("text/plain",row.id));
                            const next = Object.entries(stageLabels).filter(([key])=>manager() || !["offer","hired"].includes(key));
                            card.append(form([field("Aşama",select("stage",next,stage)),
                                ...(manager() ? [field("Kapanış",select("closure",[["","Açık"],...Object.entries(closureLabels)]))] : [])],
                                "Taşı",fields=>move(row,fields.stage,fields.closure || null)));
                        }
                        column.append(card);
                    }
                    board.append(column);
                }
                output.append(board);
                const closed = data.applications.filter(row=>row.closure);
                if (closed.length) output.append(table(["Kapanan başvuru","Neden"],closed.map(row=>[row.name,closureLabels[row.closure]])));
                if (offset > 0) output.append(action("Önceki",()=>{offset-=25;draw();}));
                if (offset+25<data.total) output.append(action("Sonraki",()=>{offset+=25;draw();}));
            } catch(error) { notice(error.message); }
        };
        picker.addEventListener("change",()=>{offset=0;draw();});
        root.append(field("Pozisyon",picker),output);
        await draw();
    }

    async function calendar(root) {
        const state = await api("ats/calendar");
        const reviewers = manager() ? (await api("ats/reviewers")).users : [{id:metadata.user_id,name:metadata.user_name}];
        const canWrite = manager() || metadata.permissions.includes("ats_schedule");
        const editor = el("div");
        root.append(calendarGrid(state.schedules),table(["Aday","Pozisyon","Başlangıç","Bitiş","Saat dilimi","Durum","İşlem"],state.schedules.map(row => [
            row.name,row.position,day(row.starts_at,row.timezone),day(row.ends_at,row.timezone),row.timezone,scheduleLabels[row.status],
            row.status === "planned" ? action("Görüşmeyi aç",()=>{
                if (canWrite && (manager() || row.interviewer_id === metadata.user_id)) {
                    showSchedule(editor,row.application_id,row,reviewers);
                } else editor.replaceChildren();
                const reminder = textarea("reminder",row.name+" · "+row.position+" · "+day(row.starts_at,row.timezone));
                reminder.readOnly = true;
                editor.append(field("Hatırlatma taslağı",reminder));
            }) : "—"
        ])),editor);
        if (canWrite) {
            const add = el("section");
            root.append(action("Yeni Görüşme Oluştur",()=>add.scrollIntoView({block:"nearest"})),add);
            await applicationPicker(add,async (id,detail)=>showSchedule(detail,id,null,reviewers));
        }
    }

    function calendarGrid(schedules) {
        const region = el("section");
        const mode = select("calendar_mode",[["month","Aylık"],["week","Haftalık"],["day","Günlük"]],"month");
        const title = el("strong"), grid = el("div","",{class:"saas-calendar-grid"});
        const zone = "Europe/Istanbul";
        const key = value => new Intl.DateTimeFormat("sv-SE",{timeZone:zone,year:"numeric",month:"2-digit",day:"2-digit"}).format(value);
        const today = new Date(key(new Date())+"T12:00:00Z");
        const jump = input("calendar_date","date",key(today));
        let focus = new Date(today);
        const addDay = (date,count) => {const result=new Date(date);result.setUTCDate(result.getUTCDate()+count);return result;};
        function render() {
            jump.value = key(focus);
            const offset = date => (date.getUTCDay()+6)%7;
            const modeValue=mode.value;
            const first = new Date(Date.UTC(focus.getUTCFullYear(),focus.getUTCMonth(),1,12));
            const start=modeValue==="month" ? addDay(first,-offset(first)) : modeValue==="week" ? addDay(focus,-offset(focus)) : focus;
            const count=modeValue==="month" ? 42 : modeValue==="week" ? 7 : 1;
            title.textContent = new Intl.DateTimeFormat("tr-TR",{timeZone:zone,month:"long",year:"numeric",
                ...(modeValue==="day" ? {day:"numeric"} : {})}).format(focus);
            grid.classList.toggle("week-grid",count>1);
            grid.replaceChildren();
            if (count>1) for (const label of ["Pzt","Sal","Çar","Per","Cum","Cmt","Paz"]) grid.append(el("strong",label));
            for(let index=0;index<count;index++){
                const date=addDay(start,index);
                const cell=el("div","",{class:"saas-calendar-cell"});
                if (modeValue==="month" && date.getUTCMonth()!==focus.getUTCMonth()) cell.classList.add("off-month");
                cell.append(el("strong",String(date.getUTCDate())));
                for(const item of schedules.filter(row=>key(new Date(row.starts_at))===key(date))){
                    const time = new Date(item.starts_at).toLocaleTimeString("tr-TR",{timeZone:zone,hour:"2-digit",minute:"2-digit"});
                    cell.append(el("span",time+" "+item.name,{class:"saas-calendar-event",title:scheduleLabels[item.status]}));
                }
                grid.append(cell);
            }
        }
        function shift(direction) {
            focus = mode.value==="month"
                ? new Date(Date.UTC(focus.getUTCFullYear(),focus.getUTCMonth()+direction,1,12))
                : addDay(focus,direction*(mode.value==="week" ? 7 : 1));
            render();
        }
        mode.addEventListener("change",render);
        jump.addEventListener("change",()=>{if(jump.value){focus=new Date(jump.value+"T12:00:00Z");render();}});
        const toolbar=el("div","",{class:"saas-calendar-toolbar"});
        toolbar.append(action("Önceki",()=>shift(-1)),action("Bugün",()=>{focus=new Date(today);render();}),
                       action("Sonraki",()=>shift(1)),field("Görünüm",mode),field("Tarihe git",jump),title,
                       el("small","Takvim saat dilimi · Europe/Istanbul"));
        region.append(toolbar,grid);
        render();
        return region;
    }

    function showSchedule(root,id,row,reviewers) {
        const zone = row?.timezone || "Europe/Istanbul";
        root.replaceChildren(form([
            field("Görüşmeci",select("interviewer_id",reviewers.map(user=>[user.id,user.name]),row?.interviewer_id || reviewers[0]?.id)),
            field("Başlangıç",input("starts_at","datetime-local",dateInput(row?.starts_at,zone),{required:""})),
            field("Bitiş",input("ends_at","datetime-local",dateInput(row?.ends_at,zone),{required:""})),
            field("Saat dilimi",input("timezone","text",zone,{required:""})),
            field("Tür",select("kind",[["onsite","Yüz yüze"],["video","Video"],["phone","Telefon"]],row?.kind || "onsite")),
            field("Notlar",textarea("notes",row?.notes || "",3000)),
            ...(row ? [field("Durum",select("status",Object.entries(scheduleLabels),row.status))] : [])
        ],row ? "Görüşmeyi güncelle" : "Görüşme planla",async data=>{
            await api(appRoute(id)+"/schedules"+(row ? "/"+row.id : ""),row ? "PATCH" : "POST",
                      {...data,interviewer_id:Number(data.interviewer_id),revision:row?.revision || 0});
            await open("calendar");
            notice("Görüşme takvime kaydedildi.");
        }));
    }

    async function evaluations(root) {
        if (manager()) {
            const list = await positions();
            if (list.length) root.append(form([
                field("Pozisyon",select("position_id",list,list[0][0])),
                field("Kriterler (satır başına bir kriter)",textarea("criteria"))
            ],"Kriterleri kaydet",async fields=>{
                await api("ats/positions/"+fields.position_id+"/criteria","PUT",
                          {criteria:fields.criteria.split("\n").map(value=>value.trim()).filter(Boolean)});
                notice("Kriterler kaydedildi.");
            }));
        }
        await applicationPicker(root,async(id,detail)=>{
            const data = await api(appRoute(id)+"/evaluations");
            if (!data.evaluations.length) detail.append(el("p","Bu başvuru için henüz değerlendirme yok.",{class:"v5-empty"}));
            detail.append(table(["Görüşmeci","Durum","Not","Güçlü yönler","Gelişim alanları","Karar"],data.evaluations.map(row=>{
                const state = !row.completed ? "Taslak" : ({pending:"Onay bekliyor",approved:"Onaylandı",rejected:"Reddedildi"})[row.state];
                const decision = manager() && row.completed && row.state==="pending" ? form([
                    field("Yönetici kararı",select("state",[["approved","Onayla"],["rejected","Reddet"]],"approved"))
                ],"Kararı kaydet",async data=>{
                    await api(appRoute(id)+"/evaluations/"+row.id+"/decision","PATCH",data);
                    await open("evaluations");
                }) : "—";
                return [row.reviewer_id,state,row.note,row.strengths,row.improvements,decision];
            })));
            if (!data.criteria.length) {detail.append(el("p","Pozisyon için kriter belirlenmedi."));return;}
            const own = data.evaluations.find(item=>item.reviewer_id===metadata.user_id);
            if (own?.completed) return;
            detail.append(form([
                ...data.criteria.map((criterion,index)=>field(criterion,input("score_"+index,"number",own?.scores[criterion] || "",
                                                                              {min:1,max:5}))),
                field("Not",textarea("note",own?.note || "",4000)),
                field("Güçlü yönler",textarea("strengths",own?.strengths || "",3000)),
                field("Gelişime açık yönler",textarea("improvements",own?.improvements || "",3000)),
                field("Değerlendirmeyi tamamla",input("completed","checkbox","1"))
            ],"Değerlendirmeyi kaydet",async fields=>{
                const scores = Object.fromEntries(data.criteria.flatMap((name,index)=>fields["score_"+index]
                    ? [[name,Number(fields["score_"+index])]] : []));
                await api(appRoute(id)+"/evaluations","PUT",{scores,note:fields.note,strengths:fields.strengths,
                    improvements:fields.improvements,completed:fields.completed==="1"});
                await open("evaluations");
            }));
        });
    }

    async function offers(root) {
        root.append(el("p","Teklif taslağı ve durum değişiklikleri burada kaydedilir. Harici gönderim yapılmaz."));
        const editor = el("div");
        root.append(action("Yeni Teklif Hazırla",()=>editor.querySelector("select")?.focus()),editor);
        await applicationPicker(editor,async(id,detail)=>{
            const {offers: rows} = await api(appRoute(id)+"/offers");
            detail.append(table(["Ücret","Başlangıç","Durum","Belge","İşlem"],rows.map(row=>{
                const base = appRoute(id)+"/offers/"+row.id;
                const allowed = nextOffers[row.status] || [];
                return [(row.amount_minor/100).toLocaleString("tr-TR")+" "+row.currency,row.starts,offerLabels[row.status],
                    link("Teklif PDF’sini indir",apiRoot+base+"/document"),allowed.length ? form([
                        field("Yeni durum",select("status",allowed.map(status=>[status,offerLabels[status]]),allowed[0]))
                    ],"Kaydet",async data=>{
                        await api(base,"PATCH",{status:data.status,revision:row.revision});
                        await open("offers");
                    }) : "—"];
            })));
            detail.append(form([field("Ücret",input("amount","number","",{min:"0.01",step:"0.01",required:""})),
                field("Para birimi",select("currency",[["TRY","TRY"],["EUR","EUR"],["USD","USD"]],"TRY")),
                field("Ücret türü",select("pay_basis",[["","Belirtilmedi"],["gross","Brüt"],["net","Net"]])),
                field("Başlangıç tarihi",input("starts","date","",{required:""})),
                field("Son geçerlilik tarihi",input("valid_until","date")),
                field("Yan haklar",textarea("benefits","",4000)),
                field("Teklif koşulları",textarea("terms","",8000))
            ],"Teklif taslağı oluştur",async data=>{
                const {amount,currency,starts,terms,pay_basis,benefits,valid_until}=data;
                await api(appRoute(id)+"/offers","POST",{amount_minor:Math.round(Number(amount)*100),currency,starts,terms,
                    pay_basis:pay_basis||null,benefits,valid_until:valid_until||null});
                await open("offers");
            }));
        });
    }

    async function reports(root) {
        const response = await fetch("/api/hr/interviews",{cache:"no-store"});
        if (!response.ok) throw new Error("AI değerlendirme raporları alınamadı.");
        const data = await response.json();
        root.append(table(["Aday","Pozisyon","Rapor"],data.interviews.filter(row=>row.status==="completed").map(row=>[
            row.name,row.position,link("PDF indir","/api/hr/interviews/"+row.token+"/pdf")
        ])));
    }

    async function settings(root) {
        const company = await api("company-settings");
        function section(title) {
            const block = el("section","",{class:"saas-settings-section"});
            block.append(el("h3",title));
            root.append(block);
            return block;
        }
        const brand = section("Kurumsal Kimlik");
        brand.append(el("p",company.name),el("p","Firma unvanı CONTROL kaydından alınır."));
        if (metadata.role === "supervisor") {
            const image = input("logo","file","",{accept:"image/png,image/jpeg",required:""});
            const preview = el("span","",{class:"saas-inline-preview",hidden:""});
            const thumbnail = el("img","",{alt:"Yüklenecek firma logosunun önizlemesi"});
            let previewUrl = "";
            preview.append(thumbnail);
            image.addEventListener("change",()=>{
                if (previewUrl) URL.revokeObjectURL(previewUrl);
                const selected = image.files[0];
                previewUrl = selected ? URL.createObjectURL(selected) : "";
                preview.hidden = !selected;
                if (selected) thumbnail.src = previewUrl;
            });
            brand.append(form([field("Firma logosu (PNG veya JPEG, en fazla 500 KB)",image)],
                "Logoyu yükle",async (_,form)=>{
                    await api("company-settings/logo","PUT",new FormData(form));
                    const logo = document.getElementById("hrCompanyLogo");
                    logo.src = "/api/ik/company-logo?v=" + Date.now();
                    form.reset();
                    if (previewUrl) URL.revokeObjectURL(previewUrl);
                    previewUrl = "";
                    preview.hidden = true;
                    notice("Firma logosu güncellendi.");
                }),preview);
        }
        section("Entegrasyonlar").append(el("p","Bu firma portalı · "+location.host),
            el("p","Firma kimliği, paket ve kota bilgileri CONTROL’den alınır."));
        const organization = section("Organizasyon");
        organization.append(el("p","Sektör · "+(company.sector || "Henüz tanımlanmadı")));
        if (metadata.role === "supervisor") {
            const sectors = ["Lojistik","Teknoloji","Üretim","Gıda","Finans","Perakende","Sağlık","Hizmet"];
            const choose = select("sector_choice",[["","Seçin"],...sectors.map(value=>[value,value]),["other","Diğer"]],
                sectors.includes(company.sector) ? company.sector : company.sector ? "other" : "");
            const custom = input("sector","text",sectors.includes(company.sector) ? "" : company.sector,{maxlength:"100"});
            const customField = field("Diğer sektör",custom);
            const sync = ()=>{ customField.hidden=choose.value!=="other"; custom.required=choose.value==="other"; };
            choose.required=true;
            choose.addEventListener("change",sync);
            sync();
            organization.append(form([field("Firma sektörü",choose),customField],
                "Sektörü kaydet",async fields=>{
                    await api("company-settings/sector","PUT",{sector:fields.sector_choice==="other"?fields.sector:fields.sector_choice});
                    await open("settings");
                }));
        }
        if (!metadata.products.includes("ats")) return;
        organization.append(action("Pozisyonlar",()=>document.getElementById("positionsMenu").click()));
        const group = await api("departments");
        const departmentForm = form([field("Departman adı",input("name","text","",{required:"",maxlength:"120"}))],
            "Departman ekle",async fields=>{
                await api("departments","POST",fields);
                await open("settings");
            });
        departmentForm.id = "departmentCreateForm";
        organization.append(el("h4","Departmanlar"),table(["Departman","İşlem"],group.departments.map(row=>[row.name,
            form([field("Yeni departman adı",input("name","text",row.name,{required:"",maxlength:"120"}))],
                "Adı güncelle",async fields=>{
                    await api("departments/"+row.id,"PATCH",fields);
                    await open("settings");
                })])),departmentForm);
        const data = await api("ats/privacy");
        section("İşe Alım Ayarları").append(el("p","Pozisyona özel kriterler ve görüşme aşamaları."),
            action("İşe Alım Panosu",()=>document.getElementById("kanbanMenu").click()),
            action("Değerlendirmeler",()=>document.getElementById("evaluationsMenu").click()));
        if (metadata.role === "supervisor") {
            section("Kullanıcılar ve yetkiler").append(action("Personel Yönetimi",()=>document.getElementById("personnelMenu").click()));
            section("Bildirimler").append(el("p","Hesap kurulum ve parola sıfırlama bildirimleri Personel Yönetimi işlemleriyle başlatılır."),
                action("Personel işlemlerini aç",()=>document.getElementById("personnelMenu").click()));
        }
        const privacy = section("Veri ve Gizlilik");
        privacy.append(form([field("Saklama süresi (gün)",input("retention_days","number",data.policy?.retention_days,{required:"",min:1,max:3650}))],
                         "Saklama politikasını kaydet",async fields=>{
            await api("ats/privacy","PUT",{retention_days:Number(fields.retention_days)});
            await open("settings");
        }));
        const requestStatus = {requested:"İnceleme bekliyor",approved:"Onaylandı · işlem bekliyor",rejected:"Reddedildi"};
        privacy.append(el("h4","Gizlilik talepleri"),table(["Tür","Durum","Tarih","İşlem"],data.requests.map(row=>[
            {erase:"Silme",anonymize:"Anonimleştirme",export:"Dışa aktarma"}[row.kind],
            requestStatus[row.status],day(row.created_at),
            row.status === "requested" ? form([
                field("Karar",select("status",[["approved","Onayla"],["rejected","Reddet"]],"approved"))
            ],"Kararı kaydet",async fields=>{
                await api("ats/privacy/requests/"+row.id,"PATCH",fields);
                await open("settings");
            }) : "—"
        ])));
    }

    async function history(root) {
        const data = await api("history");
        const label = {
            application_stage_changed:"Başvuru aşaması değiştirildi",application_created:"Başvuru oluşturuldu",
            candidate_name_updated:"Adayın ad soyadı güncellendi",
            interview_assessment_requested:"Mülakat değerlendirmesi istendi",
            position_created:"Pozisyon oluşturuldu",position_closed:"Pozisyon kapatıldı",department_created:"Departman eklendi",
            department_renamed:"Departman güncellendi",job_published:"İlan yayınlandı",offer_drafted:"Teklif taslağı oluşturuldu",
            evaluation_completed:"Değerlendirme tamamlandı",evaluation_approved:"Değerlendirme onaylandı",
            application_assigned:"Başvuru sorumlusu güncellendi",application_document_added:"Başvuru belgesi eklendi",
            application_note_added:"Başvuru notu eklendi",position_changed:"Pozisyon güncellendi",
            job_draft:"İlan taslağı kaydedildi",job_closed:"İlan kapatıldı",
            interview_link_created:"Mülakat bağlantısı oluşturuldu",schedule_planned:"Görüşme planlandı"
        };
        const events = [
            ...data.control.map(row=>({who:row.actor,action:row.action,
                type:row.action.split(" #")[0],stamp:String(row.stamp).replace(" ","T")+"Z",
                record:row.action.match(/#[0-9]+/)?.[0] || "Denetim #"+row.id})),
            ...data.recruitment.map(row=>({who:row.actor_name || "Kullanıcı #"+row.user_id,action:label[row.event] || row.event,
                type:row.event,stamp:row.recorded_at,record:row.record_name || row.target}))
        ].sort((a,b)=>Date.parse(b.stamp)-Date.parse(a.stamp));
        const types = [...new Set(events.map(row=>row.type))].sort();
        const search = input("query","search");
        const from = input("from","date");
        const to = input("to","date");
        const kind = select("kind",[["","Tüm işlemler"],...types.map(code=>[code,label[code] || code.replaceAll("_"," ")])]);
        const output=el("div");
        const formatDay = new Intl.DateTimeFormat("sv-SE",{timeZone:"Europe/Istanbul",year:"numeric",month:"2-digit",day:"2-digit"});
        const draw = fields => {
            const query = fields.query?.toLocaleLowerCase("tr-TR").trim() || "";
            const filtered=events.filter(row=>{
                const date=formatDay.format(new Date(row.stamp));
                return (!fields.from || date>=fields.from) && (!fields.to || date<=fields.to) &&
                    (!fields.kind || row.type===fields.kind) &&
                    (!query || (row.who+" "+row.action+" "+row.record).toLocaleLowerCase("tr-TR").includes(query));
            });
            output.replaceChildren(el("p",filtered.length+" işlem"),table(["Kullanıcı","İşlem","Tarih","İlgili kayıt"],
                filtered.map(row=>[row.who,row.action.replaceAll("_"," "),day(row.stamp),row.record])));
        };
        root.append(el("p","Her bölümün en güncel 100 denetim olayı."),form([
            field("Kullanıcı, işlem veya kayıt ara",search),field("Başlangıç",from),field("Bitiş",to),field("İşlem türü",kind)
        ],"Filtrele",draw),output);
        draw({});
    }
    const screens = {jobs:["İş İlanları",jobs],pool:["Aday Havuzu",pool],kanban:["İşe Alım Panosu",kanban],
        calendar:["Görüşme Takvimi",calendar],evaluations:["Değerlendirmeler",evaluations],offers:["Teklifler",offers],
        reports:["AI Değerlendirme Raporları",reports],settings:["Firma Ayarları",settings],history:["İşlem Geçmişi",history]};
    async function open(name) {
        const step = ++generation;
        const [label,render] = screens[name];
        suite.hidden = false;
        const container = el("div");
        suite.replaceChildren(el("h2",label),el("p","",{role:"status"}),container);
        notice("Yükleniyor…");
        try {
            await render(container);
            if (step === generation && suite.querySelector("[role=status]").textContent === "Yükleniyor…") notice("");
        }
        catch(error) { if (step === generation) notice(error.message,true); }
    }
    document.addEventListener("ik-portal-screen", async event=>{
        const name = event.detail.startsWith("suite_") && event.detail.slice(6);
        if (!name || !screens[name]) {suite.hidden = true;++generation;return;}
        metadata = metadata || await portalMetadataPromise;
        if (metadata) open(name);
    });
    async function loadAtsDashboard() {
        metadata = metadata || await portalMetadataPromise;
        if (!metadata?.products.includes("ats")) return;
        const onlyAts = !metadata.products.includes("ai");
        document.getElementById("dashboardHome").classList.toggle("ats-only-overview",onlyAts);
        try {
            const [metrics,recent] = await Promise.all([api("ats/metrics"),onlyAts?values():null]);
            const area = document.getElementById("atsDashboardMetrics");
            area.hidden = false;
            area.replaceChildren(el("h3","ATS özeti"));
            if (onlyAts) {
                const stats = el("div","",{class:"dashboard-stats ats-overview-stats"});
                for(const [label,value] of [["ATS Adayları",metrics.candidates],["Başvurular",metrics.applications],
                    ["Açık Pozisyonlar",metrics.open_positions],["Tamamlanan İnsan Görüşmeleri",metrics.completed_schedules]]) {
                    const item = el("div","",{class:"dashboard-stat-card"});
                    item.append(el("strong",String(value)),el("span",label));
                    stats.append(item);
                }
                const seen = new Set();
                const candidates = recent.applications.filter(row=>{
                    if (seen.has(row.candidate_id)) return false;
                    seen.add(row.candidate_id);
                    return true;
                });
                area.append(stats,el("h3","Son Adaylar"),table(["Ad soyad","Pozisyon","Aşama","İşlem"],
                    candidates.slice(0,5).map(row=>[row.name,row.position,stageLabels[row.stage],
                        action("Aday Havuzunu aç",()=>{
                            chosenApplication=row.id;
                            const menu=document.getElementById("poolMenu");
                            menu.dataset.openApplication=row.id;
                            menu.click();
                        })])));
            }
            area.append(table(["Açık pozisyon","Aday","Başvuru","Tamamlanan planlı görüşmeler","İşe alım süresi"],
                [[metrics.open_positions,metrics.candidates,metrics.applications,metrics.completed_schedules,
                  metrics.hiring_days == null ? "—" : metrics.hiring_days+" gün"]]),
                table(["İşe alım aşaması","Başvuru"],Object.entries(metrics.stages).map(([key,n])=>[stageLabels[key],n])),
                table(["Ay","Başvuru"],Object.entries(metrics.months)),
                table(["Pozisyon","Başvuru"],metrics.positions.map(row=>[row.name,row.applications])));
        } catch (_) {
            const area = document.getElementById("atsDashboardMetrics");
            area.hidden = false;
            area.textContent = "ATS özeti yüklenemedi. Verileri yenileyin.";
        }
    }
    document.getElementById("dashboardRefresh")?.addEventListener("click",loadAtsDashboard);
    document.addEventListener("ik-portal-screen",event=>{if(event.detail==="dashboard") loadAtsDashboard();});
    loadAtsDashboard();
}
