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
    // Reuse simultaneous read requests instead of sending the same expensive
    // ATS query multiple times while dashboard and panels are mounting.
    // Cache lives only in this tab, briefly, and is invalidated on every write.
    const readCache = new Map();
    const readPending = new Map();
    async function api(path, method = "GET", data) {
        const isRead = method === "GET" && data === undefined;
        const now = Date.now();
        if (isRead) {
            const cached = readCache.get(path);
            if (cached && cached.until > now) return cached.value;
            if (readPending.has(path)) return readPending.get(path);
        } else {
            readCache.clear();
        }
        const spec = {method,cache:"no-store"};
        if (data instanceof FormData) spec.body = data;
        else if (data !== undefined) {
            spec.headers = {"Content-Type":"application/json"};
            spec.body = JSON.stringify(data);
        }
        const request = portalJson(apiRoot + path,spec,"İşlem tamamlanamadı. Alanları kontrol edip tekrar deneyin.");
        if (!isRead) return request;
        readPending.set(path,request);
        try {
            const result = await request;
            // Only cache lightweight list/metadata endpoints. Never cache
            // candidate files, reports, individual records, or email state.
            if (/^(positions|assignees|ats\\/metrics|ats\\/applications\\?)/.test(path)) {
                readCache.set(path,{value:result,until:Date.now()+8000});
                if (readCache.size > 30) readCache.delete(readCache.keys().next().value);
            }
            return result;
        } finally {
            readPending.delete(path);
        }
    }
    function table(headers, rows) {
        if (!rows.length) return el("p","Bu listede henüz kayıt yok.",{class:"v5-empty"});
        const wrap = el("div","",{class:"saas-table-wrap"});
        const grid = el("table","",{class:"saas-table"});
        grid.createTHead().insertRow().append(...headers.map(label => {
            const th = el("th",label instanceof Node ? "" : label,{scope:"col"});
            if (label instanceof Node) th.append(label);
            return th;
        }));
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
    async function values(positionId = "", query = "", stage = "", offset = 0, assignee = "", nameOrder = "", classification = "") {
        return api("ats/applications?" + new URLSearchParams({position_id:positionId,search:query,stage,limit:"25",offset,assignee,name_order:nameOrder,classification}));
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

    // A job link belongs to the *current authenticated company portal*.
    // Platform-specific source labels help internal attribution but do not
    // prove that the external platform published or approved this vacancy.
    function publishedJobUrl(jobId, source = "direct") {
        const url = new URL("/basvuru/" + encodeURIComponent(jobId),window.location.origin);
        if (source !== "direct") url.searchParams.set("src",source);
        return url.toString();
    }
    async function copyPublicJobLink(jobId, source = "direct") {
        const address = publishedJobUrl(jobId,source);
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(address);
            notice("İlana özel başvuru bağlantısı kopyalandı.");
        } else {
            // Manual copy fallback in browsers without Clipboard API.
            window.prompt("Başvuru bağlantısını kopyalayın:",address);
            notice("Bağlantı kopyalama penceresinde gösterildi.");
        }
    }
    function publishedJobShareControls(job) {
        const actions = el("div","",{class:"saas-actions"});
        const applicationLink=input("public_job_url","url",publishedJobUrl(job.id),{readonly:"", "aria-label":"İlana özel başvuru bağlantısı"});
        applicationLink.addEventListener("focus",()=>applicationLink.select());
        actions.append(field("Başvuru bağlantısı",applicationLink),
            action("Bağlantıyı Kopyala",()=>copyPublicJobLink(job.id)));
        actions.append(action("Bağlantıyı Paylaş",async ()=>{
            const url = publishedJobUrl(job.id);
            if (navigator.share) {
                try { await navigator.share({title:job.title,text:"İş ilanımız: "+job.title,url}); }
                catch (error) {if (error.name !== "AbortError") notice("Paylaşım gerçekleştirilemedi.",true)}
            } else {
                await copyPublicJobLink(job.id);
            }
        }));
        const additional = el("details","",{class:"saas-share-options"});
        const heading = el("summary","LinkedIn · Kariyer.net · Eleman.net ve diğer platformlar");
        const explanation=el("p","Yetkili firma bağlantıyı kendi hesabında paylaşabilir. ERNASA, resmî platform anlaşması ve erişimi olmadan LinkedIn, Kariyer.net veya Eleman.net üzerinde otomatik ilan yayınlamaz.");
        const platform=select("sharing_source",[
            ["linkedin","LinkedIn"],["indeed","Indeed"],
            ["kariyer_net","Kariyer.net"],["eleman_net","Eleman.net"],["social","Diğer sosyal kanal"],
            ["other","Diğer kaynak"]
        ],"linkedin");
        const choice=input("platform_opt_in","checkbox","1");
        choice.checked=false;
        const copy=action("Seçilen Platformun Bağlantısını Kopyala",()=>copyPublicJobLink(job.id,platform.value));
        copy.disabled=true;
        choice.addEventListener("change",()=>{copy.disabled=!choice.checked});
        additional.append(heading,explanation,
            field("Dış platform paylaşımını isteğe bağlı etkinleştir",choice),
            field("Platform / kaynak",platform),copy);
        actions.append(additional);
        return actions;
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
        const deadline = input("closes_at","datetime-local","");
        which.required = summary.required = policy.required = version.required = true;
        const loadJob = () => {
            const job = data.jobs.find(row => String(row.position_id) === which.value);
            title.value = job?.title || "";
            deadline.value = dateInput(job?.closes_at,"Europe/Istanbul");
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
            const active = row.state === "published" &&
                choices.some(([id])=>String(id)===String(row.position_id)) &&
                (!row.closes_at || Date.parse(row.closes_at) > Date.now());
            if (active) {
                const share = publishedJobShareControls(row);
                share.hidden = true;
                actions.append(action("Bağlantı Oluştur",()=>{
                    share.hidden = false;
                    share.querySelector("input")?.focus();
                }),share);
            }
            return [row.title,row.state === "published" && !active ? "Başvuru kapalı" :
                ({draft:"Taslak",published:"Yayında",closed:"Kapalı"})[row.state],
                active ? link("İlana özel başvuru sayfasını aç","/basvuru/" + row.id) : "—",actions];
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
        root.append(el("p","İlan yayımlandığında firmaya özel başvuru bağlantısı oluşur. Firma yetkilisi kendi web sitesinde ve kurumsal kanallarında paylaşabilir. İş arama platformları isteğe bağlıdır; otomatik platform yayını yapılmaz."),
            form([field("Pozisyon",which),field("İlan başlığı",title),field("İş tanımı",summary),
            field("Aranan nitelikler",qualifications),field("Çalışma koşulları",conditions),
            field("Aday aydınlatma metni",policy),field("Metin sürümü",version),field("Son başvuru (Türkiye saati)",deadline),field("Durum",state),
            field("Yerel yayını onaylıyorum",confirm)],"İlanı kaydet",async data => {
            if (data.state === "published" && !data.closes_at)
                throw new Error("Yayın için son başvuru tarihini girin.");
            await api("ats/jobs","POST",{...data,confirm_publish:data.confirm_publish==="1"});
            await open("jobs");
            notice("İlan kaydedildi.");
        }),previewButton,preview);
    }

    const atsGroups = {suitable:"Pozisyona Uygun",development:"Gelişime Açık",
                       review:"Ek Değerlendirme Gerekli"};

    async function showStoredScreening(applicationId, documentId, container, wait = false, permit = false) {
        const route = appRoute(applicationId)+"/documents/"+encodeURIComponent(documentId)+"/screening";
        for (let attempt = 0; attempt < (wait ? 45 : 1) && container.isConnected; attempt++) {
            try {
                const result = await api(route);
                container.replaceChildren(el("p", result.state === "pending"
                    ? "ATS değerlendirmesi sırada."
                    : "ATS puanı · " + (result.score ?? "Kanıtlı puan yok") +
                      (result.coverage == null ? "" : " · Kanıt kapsamı %" + result.coverage)));
                if (result.state !== "pending" && Number.isFinite(Number(result.score)) && result.score !== null) {
                    const score = Math.max(0,Math.min(100,Number(result.score)));
                    const donut = el("div","",{class:"ernasa-ats-donut",role:"img",
                        "aria-label":"ATS uyum puanı yüzde "+score.toFixed(1)});
                    donut.style.setProperty("--ats-score",String(score)+"%");
                    const donutCenter = el("div","",{class:"ernasa-ats-donut-center"});
                    donutCenter.append(el("strong",score.toFixed(1)),el("span","ATS UYUM PUANI"));
                    donut.append(donutCenter);
                    const coverage = result.coverage == null ? "Hesaplanmadı" : "%"+result.coverage;
                    const evidenceGroups = [
                        {label:"Güçlü kanıt",color:"#17a673",count:0},
                        {label:"Kısmi eşleşme",color:"#f59e0b",count:0},
                        {label:"Kanıt bulunamadı",color:"#f05266",count:0},
                        {label:"Diğer / doğrulanacak",color:"#7b91ae",count:0}
                    ];
                    const criteria = [...(result.strengths || []),...(result.development || [])];
                    for (const item of criteria) {
                        const status = String(item.status || "").toLocaleLowerCase("tr-TR");
                        const evidence = String(item.evidence || "").toLocaleLowerCase("tr-TR");
                        if (/kanıt bulunamad|kanıt yok|eşleşme yok|doğrulanamad|not found|missing/.test(status+" "+evidence))
                            evidenceGroups[2].count++;
                        else if (/kısmi|partial|geliştirilmeli|sınırlı/.test(status))
                            evidenceGroups[1].count++;
                        else if (/güçlü|doğrulan|kanıtlı|tam eşleş|strong|matched/.test(status))
                            evidenceGroups[0].count++;
                        else evidenceGroups[3].count++;
                    }
                    const evidenceTotal = criteria.length;
                    const evidenceChart = el("div","",{class:"ernasa-evidence-chart"});
                    const evidenceRing = el("div","",{class:"ernasa-evidence-ring",role:"img",
                        "aria-label":"Kanıt dağılımı, toplam "+evidenceTotal+" kriter"});
                    const evidenceCenter = el("div","",{class:"ernasa-evidence-center"});
                    evidenceCenter.append(el("strong",String(evidenceTotal)),el("span","KRİTER"));
                    evidenceRing.append(evidenceCenter);
                    if (evidenceTotal) {
                        let angle = 0;
                        const segments = [];
                        for (const group of evidenceGroups) {
                            if (!group.count) continue;
                            const nextAngle = angle + 100*group.count/evidenceTotal;
                            segments.push(group.color+" "+angle+"% "+nextAngle+"%");
                            angle = nextAngle;
                        }
                        evidenceRing.style.background = "conic-gradient("+segments.join(",")+")";
                    }
                    const legend = el("div","",{class:"ernasa-evidence-legend"});
                    for (const group of evidenceGroups) {
                        const line = el("div","",{class:"ernasa-evidence-legend-row"});
                        const dot = el("span","",{class:"ernasa-evidence-dot"});
                        dot.style.background = group.color;
                        line.append(dot,el("span",group.label),el("strong",String(group.count)));
                        legend.append(line);
                    }
                    evidenceChart.append(evidenceRing,legend);
                    const scoreChart = el("div","",{class:"ernasa-score-chart"});
                    scoreChart.append(el("h5","ATS uyum puanı"),donut,
                        el("p","Kanıt kapsamı: "+coverage));
                    const evidencePanel = el("div","",{class:"ernasa-evidence-panel"});
                    evidencePanel.append(el("h5","Kanıt dağılımı"),evidenceChart);
                    const summary = el("div","",{class:"ernasa-ats-summary"});
                    summary.append(scoreChart,evidencePanel);
                    container.append(summary);
                    container.append(el("p","Bu grafik CV'deki kriter kanıtlarını gösterir; işe alım kararı değildir.",
                        {class:"ernasa-ats-caption"}));
                }
                if (result.state !== "pending" && result.classification)
                    container.append(el("p",atsGroups[result.classification]));
                if (result.reason) container.append(el("p",result.reason));
                for (const [label,criteria] of [["Güçlü yönler",result.strengths],["Gelişim ve doğrulama alanları",result.development]]) {
                    if (!criteria?.length) continue;
                    const list = el("ul");
                    for (const item of criteria) list.append(el("li",item.requirement+" · "+item.status+
                        (item.evidence ? " · "+item.evidence : "")));
                    container.append(el("strong",label),list);
                }
                if (permit && ["analyzed","criteria_missing","review_required"].includes(result.state)) {
                    const permission = result.permission;
                    if (!permission) {
                        container.append(action("Mülakat İçin İzin İste",async button=>{
                            button.disabled = true;
                            try {
                                await api("flow/applications/"+encodeURIComponent(applicationId)+"/permission",
                                    "POST",{document_id:documentId});
                                await showStoredScreening(applicationId,documentId,container,false,true);
                            } catch(error) {notice(error.message,true);button.disabled=false;}
                        }));
                    } else {
                        container.append(el("p",({pending:"Mülakat izni bekliyor",approved:"Mülakat izni onaylandı",
                            declined:"Mülakat izin talebi reddedildi"})[permission.status]));
                        if (manager() && permission.status === "pending") {
                            for (const [decision,label] of [["approved","İzni Onayla"],["declined","İzni Reddet"]]) {
                                container.append(action(label,async button=>{
                                    button.disabled = true;
                                    try {
                                        await api("flow/permissions/"+encodeURIComponent(permission.id)+"/decision",
                                            "POST",{decision});
                                        await showStoredScreening(applicationId,documentId,container,false,true);
                                    } catch(error) {notice(error.message,true);button.disabled=false;}
                                }));
                            }
                        }
                    }
                }
                if (result.state !== "pending" || !wait) return;
            } catch (error) {
                container.replaceChildren(el("p",error.message));
                return;
            }
            await new Promise(resolve=>setTimeout(resolve,2000));
        }
    }

    function cvImport(root, choices, openPositions, contextPosition, refresh, manualApplication, openApplication) {
        const limit = 20, maxBytes = 10 * 1024 * 1024;
        const fileInput = input("cv","file","",{accept:".pdf,.docx",multiple:"",required:""});
        const preview = el("div");
        const progress = el("p","",{role:"status"});
        const positionChoices = [["","Pozisyon seçin"],...openPositions];
        const selectionLabels = {name:"Ad soyad",email:"E-posta",phone:"Telefon",position_id:"Pozisyon"};
        root.append(el("h3","CV Yükle"),
            field("PDF veya DOCX (en fazla 20, dosya başına 10 MB)",fileInput),progress,preview);
        fileInput.addEventListener("change", async () => {
            const files = [...fileInput.files];
            if (!files.length) return;
            if (files.length > limit) {progress.textContent = "Bir seferde en fazla 20 dosya seçin."; return;}
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
                await renderPreview(rows);
            } finally { fileInput.disabled = false; }
        });

        async function renderPreview(rows) {
            const panel = el("fieldset","",{class:"ats-cv-preview","aria-label":"CV önizlemesi"});
            const summary = el("p","",{role:"status"});
            const save = action("Seçilenleri kaydet",()=>saveRows());
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
            const grid = table(["Seç","Belge","Ad soyad","E-posta","Telefon","Pozisyon","Eğitim","Deneyim","Durum"],
                rows.map(row => {
                    row.status = el("span");
                    if (!row.data) {
                        row.status.textContent = "Atlandı · " + row.error;
                        if (manualApplication && /^CV (okunamadı|içinde okunabilir metin)/.test(row.error)) {
                            row.status.append(el("p","Manuel başvuruyu oluşturup CV’yi aday detayından ekleyebilirsiniz."),
                                action("Manuel başvuru oluştur",manualApplication));
                        }
                        return ["",row.file.name,"","","","","","",row.status];
                    }
                    row.checked = input("selected","checkbox","1",{"aria-label":row.file.name + " belgesini seç"});
                    row.checked.checked = true;
                    row.checked.addEventListener("change",refreshSave);
                    const scopedChoices = contextPosition ? [[contextPosition,contextName || contextPosition]] : positionChoices;
                    row.fields = {
                        name:input("name","text",row.data.name,{maxlength:160}),
                        email:input("email","email",row.data.email,{maxlength:254}),
                        phone:input("phone","tel",row.data.phone,{maxlength:50}),
                        position_id:select("position_id",scopedChoices,contextPosition || row.data.suggested_position_id || "")
                    };
                    row.fields.position_id.disabled = Boolean(contextPosition);
                    for (const [key,control] of Object.entries(row.fields)) {
                        control.setAttribute("aria-label",selectionLabels[key] + " · " + row.file.name);
                        control.addEventListener("input",() => checkFields(row));
                    }
                    checkFields(row);
                    return [row.checked,row.file.name,...Object.values(row.fields),
                        row.data.education?.join(" · ") || "CV’de bulunamadı",
                        row.data.experience?.join(" · ") || "CV’de bulunamadı",row.status];
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
            await saveRows(true);

            async function saveRows(automatic = false) {
                const selected = ready.filter(row => row.checked.checked && !row.done &&
                    (!automatic || (row.fields.name.value.trim() && row.fields.position_id.value)));
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
                            row.status.textContent = result.status === "saved"
                                ? (result.identity_conflict ? "Ayrı kaydedildi · ortak e-posta İK doğrulaması bekliyor"
                                    : result.existing_application ? "Mevcut başvuru kullanıldı" : "Kaydedildi")
                                : "İK incelemesi · " + result.message;
                            if (result.status === "saved" && result.application_id && result.document_id) {
                                const screen = el("div");
                                row.status.append(action("Aday dosyasını aç",()=>openApplication(result.application_id)),screen);
                                void showStoredScreening(result.application_id,result.document_id,screen,true);
                                if (result.existing_application && selected.length === 1) await openApplication(result.application_id);
                            }
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
        const classification = select("classification",[["","Tüm ATS grupları"],...Object.entries(atsGroups)]);
        const assignee = select("assignee",[["","Tüm İK sorumluları"],["unassigned","Atanmamış"],...people.map(user=>[user.id,user.name])]);
        const flowResults = el("section","",{class:"saas-panel",tabindex:"-1",hidden:""});
        let flowOffset=0;
        const renderFlow = async () => {
            if (!target.value) {notice("Önce değerlendirmek istediğiniz pozisyonu seçin.",true);target.focus();return}
            flowResults.hidden=false;
            flowResults.replaceChildren(el("h3","ERNASA HR FLOW · Pozisyon ATS Analizi"),
                el("p","Gerçek CV kayıtları değerlendirilirken otomatik ret veya davet gönderimi yapılmaz."));
            try {
                const data=await api("flow/positions/"+encodeURIComponent(target.value)+"/screen?offset="+flowOffset,"POST");
                const header=el("div","",{class:"saas-actions"});
                header.append(el("strong",data.position+" · "+data.total+" başvuru"));
                const records=el("div");
                for(const c of data.candidates){
                    const panel=el("section","",{class:"saas-panel"});
                    const score=c.match_score===null?"Kanıtlı puan yok":String(c.match_score)+" / 100";
                    const coverage=c.coverage===null?"Kapsam hesaplanamadı":"Kanıt kapsamı: %"+c.coverage;
                    panel.append(el("h4",c.candidate),
                        el("p",c.position+" · "+c.source),
                        el("p",score+" · "+coverage),
                        el("p",c.note));
                    if(c.evidence.length){
                        const ul=el("ul");
                        for(const e of c.evidence){
                            ul.append(el("li",(e.requirement||"Kriter")+" — "+(e.status||"İncelenmedi")));
                        }
                        panel.append(ul);
                    }
                    const box=el("div","",{class:"saas-actions"});
                    box.append(action("Aday Dosyasını Aç",()=>{
                        chosenApplication=c.application_id;
                        revealDetails(c.application_id);
                    }));
                    if(c.permission){
                        box.append(el("span",({pending:"Mülakat izni bekliyor",approved:"Mülakat izni onaylandı",declined:"Mülakat izni reddedildi"})[c.permission.status]||c.permission.status));
                        const bookingStatus = {
                            pending_email:"Uygunluk e-postası sırada", sending:"Uygunluk e-postası işleniyor",
                            waiting:"Adayın gün ve saat seçimi bekleniyor", booked:"Görüşme kaydedildi",
                            dispatching:"Mülakat daveti işleniyor", finished:"Mülakat daveti e-posta sağlayıcısına iletildi",
                            failed:"Randevu veya e-posta işlemi İK kontrolü gerektiriyor"
                        }[c.permission.booking_status];
                        if(bookingStatus) box.append(el("span",bookingStatus));
                        if(c.permission.booked_at) box.append(el("span",
                            new Date(c.permission.booked_at).toLocaleString("tr-TR",{timeZone:"Europe/Istanbul"})));
                        if(manager()&&c.permission.status==="pending"){
                            for(const [decision,label] of [["approved","İzni Onayla"],["declined","İzni Reddet"]]){
                                box.append(action(label,async button=>{
                                    button.disabled=true;
                                    try{
                                        await api("flow/permissions/"+encodeURIComponent(c.permission.id)+"/decision","POST",{decision});
                                        notice(decision==="approved"?"İzin onaylandı. FLOW, adaydan uygun görüşme saatini isteyecek.":"İzin reddedildi; ATS başvurusu korundu.");
                                        await renderFlow();
                                    }catch(error){notice(error.message,true);button.disabled=false}
                                }));
                            }
                        }
                    }else if(c.has_cv && c.document_id && ["analyzed","review_required","criteria_missing"].includes(c.analysis_state)){
                        box.append(action("Mülakat İçin İzin İste",async button=>{
                            button.disabled=true;
                            try{
                                await api("flow/applications/"+encodeURIComponent(c.application_id)+"/permission",
                                    "POST",{document_id:c.document_id});
                                notice("Mülakat izin talebi kaydedildi. Adaya davet gönderilmedi.");
                                await renderFlow();
                            }catch(error){notice(error.message,true);button.disabled=false}
                        }));
                    }else{
                        box.append(el("span","İzin talebi için önce ATS ön değerlendirmesi gerekiyor."));
                    }
                    panel.append(box);records.append(panel);
                }
                flowResults.replaceChildren(el("h3","ERNASA HR FLOW · ATS Değerlendirmesi"),
                    header,el("p",data.note),records);
                const pager=el("div","",{class:"saas-actions"});
                if(flowOffset>0)pager.append(action("Önceki 20",()=>{flowOffset-=20;renderFlow()}));
                if(flowOffset+20<data.total)pager.append(action("Sonraki 20",()=>{flowOffset+=20;renderFlow()}));
                flowResults.append(pager);
                flowResults.focus();
            }catch(error){flowResults.append(el("p","Analiz başlatılamadı: "+error.message));notice(error.message,true)}
        };
        const results = el("section","",{class:"saas-panel",tabindex:"-1"});
        const details = el("section","",{class:"ernasa-candidate-body",tabindex:"-1",hidden:""});
        const editor = el("section","",{class:"saas-panel",hidden:""});
        const importer = el("section","",{class:"saas-panel",hidden:""});
        const reveal = () => { editor.hidden = false; editor.querySelector("[name=name]")?.focus(); };
        if (!document.getElementById("ernasa-candidate-design")) {
            const style = document.createElement("style");
            style.id = "ernasa-candidate-design";
            style.textContent = `
              .ernasa-file-window{background:#f5f8fd!important;padding:0!important;border:1px solid #dce5f1;overflow:auto;scrollbar-gutter:stable}
              .ernasa-file-toolbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;padding:14px 24px;background:#fff;border-bottom:1px solid #e3eaf4;box-shadow:0 3px 12px #122e5210}
              .ernasa-file-toolbar button{background:#123968;color:#fff;border:0;border-radius:9px;padding:10px 16px;font-weight:650;cursor:pointer}
              .ernasa-file-title{font-size:13px;font-weight:700;color:#55718e;letter-spacing:.05em}
              .ernasa-candidate-body{padding:24px!important;display:flex;flex-direction:column;gap:16px;max-width:1100px;margin:auto;background:transparent!important;border:0!important;box-shadow:none!important}
              .ernasa-candidate-body>.ernasa-profile-header{background:linear-gradient(115deg,#09254b,#174b86);border-radius:16px;padding:24px;color:white;box-shadow:0 12px 24px #0c356225}
              .ernasa-profile-header h3{color:white!important;font-size:22px!important;margin:0 0 12px!important;line-height:1.3}
              .ernasa-profile-header p{color:#dce9fa!important;margin:5px 0!important}
              .ernasa-file-card{background:#fff;border:1px solid #e0e8f3;border-radius:14px;padding:20px 22px;box-shadow:0 4px 15px #1536570b;min-width:0}
              .ernasa-ats-summary{display:grid;grid-template-columns:minmax(190px,1fr) minmax(320px,2fr);gap:18px;padding:18px 0;align-items:stretch}
              .ernasa-score-chart,.ernasa-evidence-panel{background:#f8fbff;border:1px solid #e1eaf6;border-radius:14px;padding:18px;min-width:0}
              .ernasa-score-chart{display:flex;flex-direction:column;align-items:center;gap:12px}
              .ernasa-score-chart h5,.ernasa-evidence-panel h5{font-size:15px;color:#173c6a;margin:0 0 12px}
              .ernasa-score-chart p{margin:0;color:#647a93;font-size:13px}
              .ernasa-evidence-chart{display:flex;align-items:center;justify-content:space-around;gap:24px;flex-wrap:wrap}
              .ernasa-evidence-ring{width:174px;height:174px;border-radius:50%;background:#e5ebf4;display:grid;place-items:center;flex:none}
              .ernasa-evidence-center{width:133px;height:133px;background:white;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-direction:column}
              .ernasa-evidence-center strong{font-size:38px;color:#173b68;line-height:1}
              .ernasa-evidence-center span{font-size:11px;font-weight:700;color:#7287a1;letter-spacing:.06em}
              .ernasa-evidence-legend{display:grid;gap:12px;min-width:190px;flex:1}
              .ernasa-evidence-legend-row{display:grid;grid-template-columns:12px 1fr auto;gap:10px;align-items:center;color:#304f70;font-size:13px}
              .ernasa-evidence-dot{width:11px;height:11px;border-radius:50%}
              .ernasa-evidence-legend-row strong{font-size:14px;color:#183a65}
              .ernasa-ats-caption{font-size:12px;color:#60768e;margin:0 0 12px}
              @media(max-width:740px){.ernasa-ats-summary{grid-template-columns:1fr}}

              .ernasa-ats-donut{width:176px;height:176px;flex:none;border-radius:50%;background:conic-gradient(#2265b2 var(--ats-score),#e5ebf4 0);display:grid;place-items:center}
              .ernasa-ats-donut-center{width:137px;height:137px;border-radius:50%;background:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px}
              .ernasa-ats-donut-center strong{font-size:38px;line-height:1;color:#173b68}
              .ernasa-ats-donut-center span{font-size:10px;letter-spacing:.07em;color:#637d99;font-weight:700}
              .ernasa-ats-metrics{display:grid;gap:12px;color:#28496e;font-weight:600}
              .ernasa-file-card .ernasa-subsection{border:1px solid #e6edf7;border-radius:12px;padding:16px;margin:12px 0;background:#fbfdff}
              .ernasa-file-card .ernasa-subsection-title{font-size:14px;font-weight:750;color:#1a477e;margin:0 0 12px}
              .ernasa-profile-header .ernasa-profile-tools{background:#ffffff13;border:1px solid #ffffff30;padding:20px;border-radius:12px;margin-top:16px;display:grid;grid-template-columns:1fr;gap:14px;max-width:760px}
              .ernasa-profile-tools>button{justify-self:start;min-width:180px}
              .ernasa-profile-tools>form{display:grid!important;grid-template-columns:minmax(0,1fr) 150px;gap:12px!important;align-items:end!important;margin:0!important;max-width:100%!important}
              .ernasa-profile-tools>form[hidden]{display:none!important}
              .ernasa-profile-tools>form>label,.ernasa-profile-tools>form>.saas-field{min-width:0;width:100%;margin:0}
              .ernasa-profile-tools>form input,.ernasa-profile-tools>form select{width:100%!important;box-sizing:border-box}
              .ernasa-profile-tools>form>button{width:100%!important;margin:0!important;align-self:end;min-height:42px}
              .ernasa-file-card.ernasa-notes-card>form{display:grid!important;grid-template-columns:minmax(0,1fr) 150px;gap:14px;align-items:end;max-width:100%}
              .ernasa-notes-card>form textarea{width:100%!important;min-height:115px;box-sizing:border-box}
              .ernasa-notes-card>form>button{width:100%;align-self:end;margin:0!important;min-height:42px}
              @media(max-width:650px){.ernasa-profile-tools>form,.ernasa-file-card.ernasa-notes-card>form{grid-template-columns:1fr}.ernasa-profile-tools>form>button,.ernasa-notes-card>form>button{width:100%}}

              .ernasa-profile-header .ernasa-profile-tools label{color:#eef6ff!important}
              .ernasa-profile-header .ernasa-profile-tools button{border:1px solid #ffffff55;background:#edf4ff;color:#17447a;border-radius:9px;padding:9px 16px}
              /* Compact, consistent dossier controls: no oversized action columns. */
              .ernasa-candidate-body .ernasa-file-card{padding:18px 20px}
              .ernasa-candidate-body .ernasa-file-card button{width:auto!important;min-width:0!important;max-width:100%;min-height:34px;padding:7px 14px;font-size:12px;line-height:1.35;white-space:normal;justify-self:start;align-self:end}
              .ernasa-candidate-body .ernasa-file-card input,.ernasa-candidate-body .ernasa-file-card select{min-height:36px;font-size:13px}
              .ernasa-candidate-body .ernasa-file-card form{display:grid!important;grid-template-columns:minmax(0,1fr) auto;align-items:end;gap:12px!important;margin:10px 0!important}
              .ernasa-candidate-body .ernasa-file-card form>button{margin:0!important}
              .ernasa-candidate-body .ernasa-file-card form>label{min-width:0;max-width:100%}
              .ernasa-candidate-body .ernasa-file-card form>label input,.ernasa-candidate-body .ernasa-file-card form>label select{width:100%;box-sizing:border-box}
              .ernasa-candidate-body .ernasa-file-card.ernasa-interview-card form{grid-template-columns:minmax(170px,1fr) minmax(180px,1fr) auto;align-items:end}
              .ernasa-candidate-body .ernasa-file-card.ernasa-interview-card form>p{margin:0;align-self:center;font-size:13px}
              .ernasa-candidate-body .ernasa-file-card.ernasa-privacy-card form{grid-template-columns:minmax(190px,320px) auto;justify-content:start}
              .ernasa-candidate-body .ernasa-file-card.ernasa-documents-card .saas-form{display:flex!important;flex-wrap:wrap;align-items:center;gap:12px}
              .ernasa-candidate-body .ernasa-file-card.ernasa-documents-card .saas-form p{margin:0;flex:1 1 250px;overflow-wrap:anywhere}
              .ernasa-candidate-body .ernasa-file-card.ernasa-documents-card .saas-form button{flex:none}
              .ernasa-candidate-body .ernasa-file-card.ernasa-documents-card form{grid-template-columns:minmax(220px,370px) auto;justify-content:start}
              .ernasa-candidate-body .ernasa-file-card.ernasa-documents-card form input[type=file]{font-size:12px;width:100%;box-sizing:border-box}
              .ernasa-candidate-body .ernasa-file-card.ernasa-documents-card .ernasa-subsection{padding:14px}
              .ernasa-candidate-body .ernasa-file-card.ernasa-notes-card form{grid-template-columns:minmax(0,1fr) auto;align-items:end}
              .ernasa-candidate-body .ernasa-file-card.ernasa-notes-card form button{min-width:110px!important}
              .ernasa-file-actions{align-items:center;padding:12px 18px!important}
              .ernasa-file-actions button{min-height:34px!important;padding:7px 14px!important;font-size:12px!important}
              @media(max-width:720px){.ernasa-candidate-body .ernasa-file-card form,.ernasa-candidate-body .ernasa-file-card.ernasa-interview-card form,.ernasa-candidate-body .ernasa-file-card.ernasa-privacy-card form,.ernasa-candidate-body .ernasa-file-card.ernasa-documents-card form{grid-template-columns:1fr!important}.ernasa-candidate-body .ernasa-file-card form>button{justify-self:stretch;width:100%!important}}
              .ernasa-file-card>h4{font-size:17px;color:#123765;margin:0 0 16px;padding-bottom:12px;border-bottom:1px solid #e8edf5}
              .ernasa-file-card .saas-panel{box-shadow:none!important;border:1px solid #e4ebf5;border-radius:12px}
              .ernasa-file-card form{margin:10px 0;max-width:100%;gap:12px}
              .ernasa-file-card input,.ernasa-file-card textarea,.ernasa-file-card select{border-radius:9px;max-width:100%}
              .ernasa-file-card button{border-radius:9px;min-height:35px;padding:8px 14px}
              .ernasa-file-card p{line-height:1.5;overflow-wrap:anywhere}
              .ernasa-file-card table{display:block;overflow-x:auto}
              .ernasa-file-actions{border:1px solid #f1d8d8;background:#fff;padding:18px 22px;border-radius:14px;display:flex;flex-wrap:wrap;gap:12px}
              .ernasa-file-actions button{border-radius:9px;padding:10px 16px;font-weight:700}
              .ernasa-file-actions button:last-child{background:#fff2f2;color:#a32323;border:1px solid #efb4b4}
              .ernasa-file-actions button:first-child{background:#e8f2ff;color:#16467d;border:1px solid #c8dbf4}
              @media(max-width:650px){.ernasa-candidate-body{padding:12px!important}.ernasa-file-card{padding:14px}.ernasa-file-toolbar{padding:10px 12px}.ernasa-profile-header h3{font-size:18px!important}}
            `;
            document.head.append(style);
        }
        // Open the candidate file immediately in a separate overlay, rather than
        // loading below the long applications list and scrolling several seconds later.
        const fileOverlay = el("div","",{role:"dialog","aria-modal":"true","aria-label":"Aday dosyası"});
        fileOverlay.style.cssText = "position:fixed;inset:0;z-index:2147483000;background:rgba(0,0,0,.58);display:none;align-items:center;justify-content:center;padding:16px;";
        const fileWindow = el("div","",{class:"saas-panel ernasa-file-window"});
        fileWindow.style.cssText = "background:var(--surface,#fff);color:var(--text,#18212f);width:min(1100px,100%);height:min(92vh,100%);overflow:auto;border-radius:14px;padding:20px;box-shadow:0 18px 60px rgba(0,0,0,.35);";
        const fileTop = el("div","",{class:"ernasa-file-toolbar"});
        const closeFile = action("← Aday listesine dön",() => {
            fileOverlay.style.display = "none";
            details.hidden = true;
            document.body.style.overflow = fileOverlay.dataset.previousOverflow || "";
        });
        fileTop.append(el("span","ERNASA  /  ADAY DOSYASI",{class:"ernasa-file-title"}),closeFile);
        fileWindow.append(fileTop,details);
        fileOverlay.append(fileWindow);
        document.body.append(fileOverlay);
        fileOverlay.addEventListener("click",event => { if(event.target === fileOverlay) closeFile.click(); });
        fileOverlay.addEventListener("keydown",event => {
            if(event.key === "Escape") { event.preventDefault(); closeFile.click(); }
        });
        let fileRequest = 0;
        const revealDetails = async id => {
            const current = ++fileRequest;
            fileOverlay.dataset.previousOverflow = document.body.style.overflow;
            document.body.style.overflow = "hidden";
            fileOverlay.style.display = "flex";
            fileWindow.scrollTop = 0;
            details.hidden = false;
            details.replaceChildren(el("p","Aday dosyası yükleniyor…"));
            closeFile.focus();
            await showApplication(details,id,draw);
            if(current === fileRequest) fileWindow.scrollTop = 0;
        };
        let offset = 0, request = 0, visible = [], nameOrder = "";
        const draw = async () => {
            const current = ++request;
            try {
                const data = await values(target.value,search.value,stage.value,offset,assignee.value,nameOrder,classification.value);
                if (current !== request) return;
                visible = data.applications;
                results.replaceChildren(el("h3","Başvurular"),el("p",data.total + " başvuru"),data.total === 0
                    ? el("p","Henüz aday bulunmuyor.",{class:"v5-empty"}) : table([action("Ad Soyad",()=>{nameOrder=nameOrder==="asc"?"desc":"asc";offset=0;draw();}),
                        "E-posta","Pozisyon","Kaynak","Aşama","ATS grubu","İşlem"],
                    data.applications.map(row => [row.name,row.identity_conflict_email
                        ? row.identity_conflict_email+" · İK doğrulaması gerekli" : row.email,row.position,row.source,closureLabels[row.closure] || stageLabels[row.stage],
                        (atsGroups[row.ats_classification] || "—") + (row.ats_score == null ? "" : " · " + row.ats_score),
                        action("Dosyayı aç",async () => {
                            chosenApplication = row.id;
                            await revealDetails(row.id);
                        })])));
                const nameHeader = results.querySelector("th");
                if (nameHeader) nameHeader.setAttribute("aria-sort", nameOrder === "asc" ? "ascending" :
                    nameOrder === "desc" ? "descending" : "none");
                if (offset > 0) results.append(action("Önceki",() => {offset -= 25;draw();}));
                if (offset + 25 < data.total) results.append(action("Sonraki",() => {offset += 25;draw();}));
            } catch (error) { notice(error.message); }
        };
        classification.addEventListener("change",()=>{offset=0;draw();});
        let searchTimer;
        search.addEventListener("input",()=>{
            clearTimeout(searchTimer);
            searchTimer = setTimeout(()=>{if(root.isConnected){offset=0;draw();}},250);
        });
        const openPositions = await positions(true);
        const cvInput = cvImport(importer,choices,openPositions,contextPosition,
            async () => {offset=0;await draw();},openPositions.length ? reveal : null,revealDetails);
        const actions = el("div","",{class:"saas-actions"});
        if (openPositions.length) actions.append(action("Yeni Aday Ekle",reveal));
        actions.append(action("CV'leri Getir ve ATS Analizi Yap",async button=>{
            button.disabled=true;
            flowOffset=0;
            try {await renderFlow()}finally{button.disabled=false}
        }));
        actions.append(action("Başvuruları Listele",async()=>{offset=0;await draw();results.focus();}),action("CV Yükle",()=>{
            importer.hidden = false;
            cvInput.focus();
        }));
        root.append(actions,form([field("Ad veya e-posta",search),field("Pozisyon",target),field("Aşama",stage),
                                  field("İK sorumlusu",assignee),field("ATS grubu",classification)],
                         "Filtrele",() => {offset=0;return draw();}),flowResults,importer,results,editor);
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
                if (row.identity_conflict_email || !row.email) {
                    root.append(el("p",row.identity_conflict_email ? "Paylaşılan e-posta · "+row.identity_conflict_email+
                        ". Adayın kendi adresini doğrulayın." : "Adayın e-posta adresi eksik.",{class:"saas-warning"}),
                        form([field("Doğrulanmış e-posta",input("email","email","",{required:"",maxlength:254}))],
                            "E-postayı doğrula",async fields => {
                                await api(appRoute(id)+"/verified-email","PATCH",fields);
                                await Promise.all([reload(),onNameChanged()]);
                            }));
                }
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
            const {documents: availabilityRows} = await api(appRoute(id)+"/document-availability");
            const availabilityById = new Map(availabilityRows.map(item => [item.id,item]));
            const cvDocuments = data.documents.filter(doc => /\.(pdf|docx)$/i.test(doc.filename || ""));
            const cvNamed = cvDocuments.filter(doc => /cv|özgeçmiş|ozgecmis|resume/i.test(doc.filename || ""));
            // Öncelik CV olarak adlandırılmış belgelerde; aksi hâlde son yüklenen PDF/DOCX.
            const lastUploaded = docs => [...docs].sort((a,b) =>
                (Date.parse(a.created_at) || 0) - (Date.parse(b.created_at) || 0)).at(-1);
            const selectedCv = lastUploaded(cvNamed.length ? cvNamed : cvDocuments);
            const latestCv = lastUploaded(cvDocuments);
            if (latestCv) {
                const stored = el("section","",{class:"saas-panel"});
                const summary = el("div");
                stored.append(el("h4","Otomatik ATS değerlendirmesi"),summary);
                root.append(stored);
                void showStoredScreening(id,latestCv.id,summary,true,!row.closure);
            }


            async function analyzeCvMatch(doc, anchor, button, suppliedText = null) {
                button.disabled = true;
                try {
                    const route = appRoute(id)+"/documents/"+encodeURIComponent(doc.id);
                    // Unreadable PDF text can be supplied by an authorized reviewer;
                    // never upload a new candidate or overwrite the original CV.
                    const matching = suppliedText === null
                        ? await api(route+"/match")
                        : await api(route+"/match-text","POST",{text:suppliedText});
                    const evidence = el("div","",{class:"ats-evidence",tabindex:"-1"});
                    evidence.append(el("h4","CV–Pozisyon Eşleşme Sonucu"),
                        el("p","İncelenen CV: "+doc.filename+" · Pozisyon: "+row.position));
                    if (matching.source === "stored_interview_cv_text") {
                        evidence.append(el("p","Özgün PDF/DOCX dosyası sunucuda bulunmuyor. Bu analiz, aynı CV belgesine bağlı mülakatta saklanmış metinden yapıldı. Dosyayı yeniden açabilmek için orijinal CV'yi aynı başvuruya tekrar yükleyin.",{class:"saas-warning"}));
                    }
                    if (matching.source === "reviewer_transcribed_cv_text") {
                        evidence.append(el("p","Analiz, İK yetkilisinin PDF'den aktardığı metne dayanmaktadır. Metnin orijinal belgeyle doğrulanması gerekir; otomatik işe alım veya ret kararı değildir.",{class:"saas-warning"}));
                    }
                    if (matching.text_extraction_status === "unreadable") {
                        evidence.append(el("p","Bu PDF'nin metin katmanı sunucuda okunamıyor. Sunucu belleğini zorlayan OCR devre dışı. Orijinal PDF'yi açıp yalnızca iş deneyimleri, beceriler ve sertifikalar bölümündeki metni aşağıya yapıştırabilirsiniz."));
                        const manual = textarea("manual_cv_text","",40000);
                        manual.placeholder = "PDF'den kopyaladığınız mesleki deneyim ve yetkinlikler (en az 10 karakter)";
                        manual.setAttribute("aria-label","CV'den kopyalanan mesleki metin");
                        const manualForm = form([field("CV metnini yapıştır",manual)],
                            "Bu metinle analiz et",async () => {
                                const provided = manual.value.trim();
                                if (provided.length < 10) throw new Error("En az 10 karakter CV metni girin.");
                                await analyzeCvMatch(doc,anchor,button,provided);
                            });
                        evidence.append(manualForm);
                    }
                    if (matching.match_score === null) {
                        evidence.append(el("p","CV–pozisyon uyum yüzdesi hesaplanamadı."));
                        evidence.append(el("p",matching.reason || "CV kanıtı veya pozisyon kriterleri eksik."));
                        if (!matching.requirements?.length) {
                            evidence.append(el("p","Çözüm: Pozisyonlar > ilgili pozisyonu Düzenle > CV–Pozisyon Eşleşme Kriterleri alanından iş gereksinimlerini ve ağırlıkları kaydedin."));
                        }
                    } else {
                        evidence.append(el("p","CV–pozisyon eşleşme puanı · %" + matching.match_score));
                    }
                    if (matching.evidence_coverage !== null) {
                        evidence.append(el("p","CV kanıt kapsamı · %" + matching.evidence_coverage +
                            " (tanımlı kriter ağırlıklarının kanıt bulunan bölümü)"));
                        if (matching.evidence_coverage < 100) {
                            evidence.append(el("p","Dikkat: Kapsam eksik. Eşleşme puanı yalnızca kanıt bulunan kriterlerden hesaplanır; tüm gereksinimler için kesin uyum yüzdesi değildir."));
                        }
                    }
                    evidence.append(table(["Kategori","İş gereksinimi","Ağırlık","Durum","CV dayanağı"],
                        (matching.requirements || []).map(item => [item.category,item.requirement,item.weight,item.status,
                            (item.evidence_items || []).map(value=>value.text+(value.page ? " (s. "+value.page+")" : "")).join(" · ")])));
                    evidence.append(el("p","Bu gösterge otomatik işe alım/ret kararı değildir. Kanıt bulunamadı durumu, adayın o beceriye sahip olmadığını kanıtlamaz. Nihai değerlendirme İK uzmanına aittir."));
                    root.querySelectorAll(".ats-evidence").forEach(item => item.remove());
                    anchor.after(evidence);
                    evidence.focus({preventScroll:true});
                    evidence.scrollIntoView({block:"nearest",behavior:"smooth"});
                } catch(error) {
                    notice(error.message,true);
                } finally {
                    button.disabled = false;
                }
            }
            if (selectedCv) {
                const quickMatch = el("div","",{class:"saas-form"});
                quickMatch.append(el("p","Otomatik seçilen CV: "+selectedCv.filename +
                    (cvDocuments.length>1 ? " · Başka belge kullanmak için aşağıdaki belge satırından inceleyebilirsiniz." : "")));
                quickMatch.append(action("CV Uyumunu Analiz Et",button => analyzeCvMatch(selectedCv,quickMatch,button)));
                root.append(quickMatch);
            } else {
                root.append(el("p","Bu ATS başvurusuna bağlı PDF/DOCX CV bulunmuyor. Önce aşağıdan CV yükleyin; ardından analiz yapabilirsiniz."));
            }
            for (const doc of data.documents) {
                const item = el("p");
                const health = availabilityById.get(doc.id);
                const available = health && ["disk_verified","archived"].includes(health.status);
                if (available) {
                    item.append(link(doc.filename,apiRoot+appRoute(id)+"/documents/"+encodeURIComponent(doc.id)));
                    item.append(document.createTextNode(" · Dosya erişilebilir"));
                } else {
                    item.append(el("strong",doc.filename));
                    const status = health?.status === "text_only"
                        ? " · Özgün dosya bulunamadı; mülakatta saklanan CV metniyle analiz yapılabilir."
                        : " · Özgün CV dosyası bulunamadı. Aşağıdaki CV yükleme alanından aynı dosyayı yeniden yükleyin; mevcut başvuru korunur.";
                    item.append(document.createTextNode(status));
                }
                if (cvDocuments.some(candidate => candidate.id === doc.id) && (available || health?.status === "text_only")) {
                    item.append(document.createTextNode(" · "));
                    item.append(action(available ? "CV eşleşmesini incele" : "Saklanan metinden analiz et",
                        button => analyzeCvMatch(doc,item,button)));
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
            if (manager() && row.candidate_id) {
                const controls = el("div","",{class:"saas-actions"});
                controls.append(action("Arşivle",async button => {
                    if (!window.confirm(row.name+" adlı adayı arşivlemek istiyor musunuz? CV ve mülakat kayıtları korunacak.")) return;
                    button.disabled = true;
                    try {
                        await api("ats/candidates/"+encodeURIComponent(row.candidate_id)+"/archive","POST",{});
                        notice("Aday arşivlendi. Kayıtlar silinmedi.");
                        await reload();
                    } catch(error) { notice(error.message,true); button.disabled=false; }
                }));
                controls.append(action("Kalıcı Sil",async button => {
                    if (!window.confirm("Yalnızca "+row.name+" adlı adayı ve bağlı ATS dosyalarını kalıcı silmek istiyor musunuz? Bu işlem geri alınamaz.")) return;
                    const confirmedName = window.prompt("Onaylamak için adayın adını aynen yazın:", "");
                    if (confirmedName !== row.name) {
                        notice("Aday adı eşleşmedi; hiçbir kayıt silinmedi.",true);
                        return;
                    }
                    button.disabled = true;
                    try {
                        await api("ats/candidates/"+encodeURIComponent(row.candidate_id)+"/permanent-delete","POST",
                            {confirmed_name:confirmedName});
                        notice("Seçilen aday kalıcı silindi.");
                        root.replaceChildren(el("p","Aday kaydı silindi."));
                        if (typeof onNameChanged === "function") await onNameChanged();
                    } catch(error) { notice(error.message,true); button.disabled=false; }
                }));
                root.append(controls);
            }
            if (manager()) root.append(link("Başvuru verisini dışa aktar",apiRoot+appRoute(id)+"/export"),
                form([field("Gizlilik talebi",select("kind",[["erase","Silme talebi"],["anonymize","Anonimleştirme"],["export","Dışa aktarma"]],"erase"))],
                     "Talebi kaydet",async fields => {
                        await api(appRoute(id)+"/privacy-requests","POST",fields);
                        notice("Talep inceleme için kaydedildi.");
                     }));
            // Reorganize existing controls into clearly labeled, task-based sections.
            const nodes = Array.from(root.children);
            let currentCard = null;
            const header = el("section","",{class:"ernasa-profile-header"});
            let interviewCard = null;
            let docsCard = null;
            let adminActions = null;
            const privacyCard = el("section","",{class:"ernasa-file-card ernasa-privacy-card"});
            privacyCard.append(el("h4","Veri yönetimi ve gizlilik"));
            let privacyHasContent = false;
            for (const node of nodes) {
                if (node.tagName === "H4") {
                    currentCard = el("section","",{class:"ernasa-file-card"});
                    root.append(currentCard);
                    if (node.textContent === "Notlar") currentCard.classList.add("ernasa-notes-card");
                    if (node.textContent === "Belgeler") currentCard.classList.add("ernasa-documents-card");
                    if (node.textContent === "AI mülakatlar") currentCard.classList.add("ernasa-interview-card");
                    if (node.textContent === "Belgeler") docsCard = currentCard;
                    if (node.textContent === "AI mülakatlar") interviewCard = currentCard;
                }
                if (!currentCard) {
                    if (node.tagName === "FORM" || node.tagName === "BUTTON") {
                        const tools = header.querySelector(".ernasa-profile-tools") ||
                            el("div","",{class:"ernasa-profile-tools"});
                        tools.append(node);
                        if (!tools.isConnected) header.append(tools);
                    } else header.append(node);
                } else if (node.classList.contains("saas-actions") &&
                    [...node.querySelectorAll("button")].some(b => b.textContent.includes("Kalıcı Sil"))) {
                    adminActions = el("section","",{class:"ernasa-file-actions"});
                    adminActions.append(node);
                } else if ((node.tagName === "A" && node.textContent.includes("Başvuru verisini dışa aktar")) ||
                    (node.tagName === "FORM" && node.textContent.includes("Gizlilik talebi"))) {
                    privacyCard.append(node);
                    privacyHasContent = true;
                } else currentCard.append(node);
            }
            root.prepend(header);
            if (docsCard) {
                const children = [...docsCard.children];
                const heading = children.shift();
                const atsSection = el("section","",{class:"ernasa-subsection"});
                atsSection.append(el("p","ATS DEĞERLENDİRMESİ",{class:"ernasa-subsection-title"}));
                const cvSection = el("section","",{class:"ernasa-subsection"});
                cvSection.append(el("p","CV BELGELERİ VE ANALİZ",{class:"ernasa-subsection-title"}));
                let cvStarted = false;
                for (const child of children) {
                    if (child.tagName === "SECTION" && child.querySelector("h4")?.textContent === "Otomatik ATS değerlendirmesi") {
                        atsSection.append(child);
                    } else {
                        cvStarted = true;
                        cvSection.append(child);
                    }
                }
                docsCard.replaceChildren(heading);
                if (atsSection.children.length > 1) docsCard.append(atsSection);
                if (cvStarted) docsCard.append(cvSection);
            }
            if (privacyHasContent) root.append(privacyCard);
            if (adminActions) root.append(adminActions);
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

    // Native IK V5 dashboard for HR FLOW: observe operations, prioritize
    // manager-only decisions. Never imply an email was delivered when prepared.
    async function flow(root) {
        root.append(el("p","İK onayından sonra FLOW adaydan uygun görüşme saatini ister ve teyit edilen randevu için davet gönderir."));
        const overview=el("section","",{class:"saas-panel"});
        const decisions=el("section","",{class:"saas-panel"});
        const casesArea=el("section","",{class:"saas-panel"});
        root.append(action("Durumu Yenile",()=>open("flow")),overview,decisions,casesArea);
        const [intake,cases]=await Promise.all([
            api("selin/intake"),api("selin/cases")
        ]);
        const entries=intake.applications||[];
        const processes=cases.cases||[];
        const exceptions=entries.filter(row=>row.status==="needs_attention");
        const pending=entries.filter(row=>row.status==="pending_review");
        const stats=el("div","",{class:"dashboard-stats"});
        for(const [label,value] of [
            ["Başvurular",entries.length],["İlk Aşama Bekleyen",pending.length],
            ["Teknik Müdahale",exceptions.length],["FLOW Dosyası",processes.length]
        ]){
            const card=el("div","",{class:"dashboard-stat-card"});
            card.append(el("strong",String(value)),el("span",label));
            stats.append(card);
        }
        overview.append(el("h3","HR FLOW Operasyon Merkezi"),stats);
        decisions.append(el("h3","İK Karar Merkezi"));
        if(!pending.length&&!exceptions.length)
            decisions.append(el("p","İlk aşamada bekleyen veya teknik müdahale gerektiren kayıt yok."));
        for(const item of [...exceptions,...pending].slice(0,80)){
            const card=el("article","",{class:"saas-panel"});
            card.append(el("strong",(item.name||"Aday")+" · "+(item.position||"Pozisyon belirtilmedi")),
                el("p","Durum: "+(item.status==="needs_attention"?"Teknik müdahale gerekli":"İlk aşama henüz otomatik tamamlanmadı")),
                el("small","Kaynak: "+(item.source||"—")));
            if(item.error_code)card.append(el("p","Hata kodu: "+item.error_code));
            const detail=el("div");
            card.append(action("Adayın İletişim Durumu",async button=>{
                button.disabled=true;
                try{
                    const result=await api("selin/intake/"+encodeURIComponent(item.application_id)+"/contact-status");
                    const outbox=(result.messages||[]).map(row=>row.purpose+" · "+row.channel+" · "+row.status);
                    detail.replaceChildren(el("p","Aday randevu teyidi: "+(result.appointment?.confirmed_by==="candidate"?"Mevcut":"Bekliyor")),
                        el("p","Kayıtlı iletişim: "+(outbox.join(", ")||"Yok")));
                }catch(error){detail.replaceChildren(el("p","Durum alınamadı: "+error.message));}
                finally{button.disabled=false;}
            }),detail);
            decisions.append(card);
        }
        casesArea.append(el("h3","FLOW İşlem Kanıtları"));
        if(!processes.length)casesArea.append(el("p","Henüz bir FLOW dosyası oluşturulmadı."));
        for(const item of processes.slice(0,40)){
            const card=el("article","",{class:"saas-panel"});
            const details=el("div");
            card.append(el("strong","Dosya · "+item.status),
                el("p","Son işlem: "+(item.updated_at||"—")));
            if(item.error_code)card.append(el("p","Kontrol kodu: "+item.error_code));
            card.append(action("İşlem Kanıtlarını Gör",async button=>{
                button.disabled=true;
                try{
                    const result=await api("selin/cases/"+encodeURIComponent(item.id)+"/evidence");
                    details.replaceChildren(...(result.steps||[]).map(step=>
                        el("p",(step.title||step.code)+" · "+step.state+
                            (step.detail?" · "+step.detail:""))));
                }catch(error){details.replaceChildren(el("p","Kanıt alınamadı: "+error.message));}
                finally{button.disabled=false;}
            }),details);
            if(item.report_available){
                card.append(el("a","Mülakat PDF Raporunu İndir",{
                    href:"/api/ik/selin/cases/"+encodeURIComponent(item.id)+"/report.pdf"
                }));
            }
            casesArea.append(card);
        }
    }

    const screens = {flow:["ERNASA HR FLOW",flow],jobs:["İş İlanları",jobs],pool:["Aday Havuzu",pool],kanban:["İşe Alım Panosu",kanban],
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
