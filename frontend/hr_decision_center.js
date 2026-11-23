"use strict";
/* ERNASA HR FLOW · İK Karar Merkezi (3 panel)
 * Sol: adaylar · Orta: sekmeli aday dosyası · Sağ: İK kararı.
 * Veriler mevcut ATS / HR FLOW / takvim / Akademi kayıtlarından gelir.
 * Nihai karar yalnızca firma yöneticisine aittir. Karar sonrası aday bilgilendirmesi mevcut e-posta kuyruğuyla yapılır.
 */
(function () {
    const API = "/api/ik/";
    const TABS = [
        ["overview", "Genel Değerlendirme"], ["cv", "Orijinal CV"], ["ats", "ATS Analizi"],
        ["report", "AI Mülakat Raporu"], ["qa", "Sorular ve Cevaplar"], ["history", "Süreç Geçmişi"],
        ["training", "Eğitim ve İlerleme"], ["notifications", "Bildirimler"],
    ];
    const DECISIONS = [
        ["approve", "ONAYLA", "hdc-approve"], ["reject", "REDDET", "hdc-reject"],
        ["second_interview", "İKİNCİ GÖRÜŞME", "hdc-second"], ["assign_training", "EĞİTİM ATA", "hdc-training"],
    ];
    const STEP_STATE = {done: ["Gerçekleşti", "ok"], waiting: ["Bekliyor", "wait"], missing: ["Kayıt yok", "none"],
        failed: ["Başarısız", "fail"], not_applicable: ["Uygulanmaz", "none"]};
    const STAGES = {new: "Yeni Başvuru", screening: "Ön Eleme", shortlist: "Kısa Liste", interview: "Görüşme",
        evaluation: "Değerlendirme", offer: "Teklif", hired: "İşe Alındı"};
    const STATE_TONE = {awaiting_hr: "warn", approved: "ok", hired: "ok", rejected: "fail", closed: "muted",
        second_interview: "info", training_assigned: "teal"};
    const AGGREGATE = [["", "Tümü"], ["awaiting_hr", "Karar bekliyor"], ["active", "Süreçte"],
        ["second_interview", "İkinci görüşme"], ["training_assigned", "Eğitimde"], ["approved", "Onaylandı"],
        ["rejected", "Reddedildi"]];
    const ACTIVE = ["application", "ats_done", "permission_pending", "availability_pending", "availability_sent",
        "appointment_selected", "interview_invited", "interview_opened", "interview_in_progress", "report_pending"];

    function el(tag, attrs = {}, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs || {})) {
            if (value === null || value === undefined || value === false) continue;
            if (key === "class") node.className = value;
            else if (key === "text") node.textContent = value;
            else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
            else node.setAttribute(key, value === true ? "" : value);
        }
        for (const child of children.flat()) {
            if (child === null || child === undefined || child === false) continue;
            node.append(child instanceof Node ? child : document.createTextNode(String(child)));
        }
        return node;
    }
    const when = value => value ? new Date(value).toLocaleString("tr-TR", {timeZone: "Europe/Istanbul",
        day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit"}) : "—";
    const score = value => value === null || value === undefined ? "—" : String(Math.round(value));
    const newKey = () => (window.crypto?.randomUUID ? crypto.randomUUID()
        : Date.now().toString(36) + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2));

    async function request(path, options = {}) {
        if (typeof window.portalJson === "function")
            return window.portalJson(API + path, {cache: "no-store", ...options}, "İşlem tamamlanamadı.");
        const response = await fetch(API + path, {cache: "no-store", ...options});
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "İşlem tamamlanamadı.");
        return body;
    }
    const post = (path, data) => request(path, {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(data)});

    function list(items, empty = "Kayıt yok.") {
        return items && items.length ? el("ul", {class: "hdc-bullets"}, items.map(item => el("li", {text: item})))
            : el("p", {class: "hdc-muted", text: empty});
    }
    // AI mülakat raporu: dört ayrı değerlendirme alanı + Genel İK Değerlendirme Özeti
    const REPORT_CARDS = [["strengths", "Güçlü Yönler", "hdc-rc-strength"], ["weaknesses", "Zayıf Yönler", "hdc-rc-weak"],
        ["development", "Geliştirilmesi Gereken Yönler", "hdc-rc-develop"], ["attention", "Dikkat Edilmesi Gerekenler", "hdc-rc-attention"]];
    function findings(items, none) {
        if (!items || !items.length) return el("p", {class: "hdc-muted hdc-none", text: none});
        return el("ul", {class: "hdc-bullets"}, items.map(item => {
            const match = /^(.*?)\s*\((Kanıt:[^)]*)\)\s*\.?$/.exec(item);
            return match ? el("li", {}, match[1], el("span", {class: "hdc-evidence", text: match[2]}))
                : el("li", {text: item});
        }));
    }
    function reportBlock(r, interviewStatus) {
        if (!r || !r.ready) {
            const why = !r ? "Mülakat bağlantısı oluşturulmadı."
                : interviewStatus !== "completed" ? "Mülakat henüz tamamlanmadı (durum: " + (interviewStatus || "—") + ")."
                : "Mülakat tamamlandı; AI raporu hazırlanıyor" + (r.assessment_status ? " (durum: " + r.assessment_status + ")." : ".");
            return el("div", {class: "hdc-report-pending", role: "status"},
                el("b", {text: "AI mülakat raporu henüz hazır değil."}), el("p", {class: "hdc-muted", text: why}));
        }
        const none = r.no_finding || "Bu alanda doğrulanmış bulgu bulunmuyor.";
        return el("section", {class: "hdc-report-block", "aria-label": "AI mülakat raporu değerlendirmesi"},
            r.legacy_notice ? el("p", {class: "hdc-legacy", text: r.legacy_notice}) : null,
            el("div", {class: "hdc-report-cards"}, ...REPORT_CARDS.map(([key, title, cls]) =>
                el("div", {class: "hdc-card hdc-rc " + cls, "data-section": key},
                    el("h4", {text: title}), findings(r[key], none)))),
            el("div", {class: "hdc-card hdc-rc hdc-rc-summary", "data-section": "summary"},
                el("h4", {text: "Genel İK Değerlendirme Özeti"}), findings(r.summary, none)),
            el("p", {class: "hdc-muted hdc-small", text: "AI raporu karar desteğidir; işe alım veya ret kararı vermez. Nihai karar yetkili İK personeline aittir."}));
    }
    function table(head, rows) {
        if (!rows.length) return el("p", {class: "hdc-muted", text: "Kayıt yok."});
        return el("div", {class: "hdc-table-wrap"}, el("table", {class: "hdc-table"},
            el("thead", {}, el("tr", {}, head.map(h => el("th", {scope: "col", text: h})))),
            el("tbody", {}, rows.map(r => el("tr", {}, r.map(c => el("td", {}, c instanceof Node ? c : (c ?? "—"))))))));
    }
    const atsLine = ats => ats.match_score === null || ats.match_score === undefined
        ? el("span", {text: "Puan üretilmedi (kanıt veya pozisyon kriteri yetersiz)"})
        : el("span", {}, el("b", {text: score(ats.match_score)}), " puan · kanıt kapsamı %" + score(ats.coverage));
    const pill = (text, tone = "") => el("span", {class: "hdc-pill " + (tone ? "hdc-" + tone : ""), text});

    function mount(root, options = {}) {
        const state = {data: null, detail: null, selected: null, filter: options.filter ?? "awaiting_hr", query: "",
            tab: "overview", cvUrl: null, trainingLinks: {}, assignees: null, catalog: null, flash: null};
        const shell = el("div", {class: "hdc"});
        const counters = el("div", {class: "hdc-counters", role: "group", "aria-label": "Süreç sayaçları ve filtre"});
        const tools = el("div", {class: "hdc-headbar"},
            el("div", {class: "hdc-title"}, el("strong", {text: "ERNASA HR FLOW · İK Karar Merkezi"}),
                el("span", {class: "hdc-muted", text: "Nihai karar yetkili İK yöneticisine aittir."})),
            counters,
            el("button", {type: "button", class: "hdc-btn", onclick: () => load(true)}, "Yenile"));
        const left = el("section", {class: "hdc-panel hdc-left", "aria-label": "Aday listesi"});
        const center = el("section", {class: "hdc-panel hdc-center", "aria-label": "Aday değerlendirmesi"});
        const right = el("aside", {class: "hdc-panel hdc-right", "aria-label": "İK karar ve işlem alanı"});
        const search = el("input", {type: "search", class: "hdc-input", placeholder: "Ad, e-posta veya pozisyon ara",
            "aria-label": "Aday ara", oninput: e => { state.query = e.target.value; drawList(); }});
        const filter = el("select", {class: "hdc-input", "aria-label": "Duruma göre filtrele",
            onchange: e => { state.filter = e.target.value; drawCounters(); drawList(); }});
        const listBox = el("div", {class: "hdc-list", role: "list"});
        left.append(el("div", {class: "hdc-list-tools"}, search, filter), listBox);
        const live = el("div", {class: "hdc-sr", "aria-live": "polite"});
        shell.append(tools, el("div", {class: "hdc-grid"}, left, center, right), live);
        root.replaceChildren(shell);
        center.append(el("div", {class: "hdc-empty"}, el("h3", {text: "Aday seçilmedi"}),
            el("p", {text: "Soldaki listeden bir aday seçin."})));
        right.append(el("p", {class: "hdc-empty", text: "İK kararları aday seçildiğinde burada görünür."}));

        function fitHeight() {
            if (window.innerWidth <= 820) { shell.style.removeProperty("--hdc-height"); return; }
            // Bileşen sayfanın görünür alanına sığar; sığmıyorsa tam görünüm yüksekliğini kullanır
            // (uzun içerik yalnızca panellerin içinde kayar, sayfa gereksiz yere uzamaz).
            const top = shell.getBoundingClientRect().top + window.scrollY;
            const below = window.innerHeight - top - 16;
            const height = below >= 600 ? below : Math.max(480, window.innerHeight - 16);
            shell.style.setProperty("--hdc-height", height + "px");
        }
        window.addEventListener("resize", fitHeight);

        function matches(item, code = state.filter, query = state.query) {
            const okState = !code || (code === "active" ? ACTIVE.includes(item.state)
                : code === "rejected" ? ["rejected", "closed"].includes(item.state)
                : code === "approved" ? ["approved", "hired"].includes(item.state) : item.state === code);
            const q = query.trim().toLocaleLowerCase("tr-TR");
            return okState && (!q || [item.name, item.email, item.position].join(" ").toLocaleLowerCase("tr-TR").includes(q));
        }
        const countFor = code => (state.data?.candidates || []).filter(item => matches(item, code, "")).length;
        function drawCounters() {
            counters.replaceChildren(...AGGREGATE.map(([code, label]) => el("button", {type: "button",
                class: "hdc-chip" + (code === "awaiting_hr" ? " hdc-chip-hr" : ""), "aria-pressed": String(state.filter === code),
                onclick: () => { state.filter = code; filter.value = code; drawCounters(); drawList(); }},
                el("b", {text: String(countFor(code))}), label)));
            filter.replaceChildren(...AGGREGATE.map(([code, label]) => el("option", {value: code, text: label})),
                el("optgroup", {label: "Ayrıntılı durum"}, (state.data?.states || []).map(s =>
                    el("option", {value: s.code, text: s.label + " (" + (state.data.counts[s.code] || 0) + ")"}))));
            filter.value = state.filter;
        }
        function drawList() {
            const rows = (state.data?.candidates || []).filter(item => matches(item));
            if (!rows.length) { listBox.replaceChildren(el("p", {class: "hdc-empty", role: "listitem", text: "Bu filtrede aday yok."})); return; }
            listBox.replaceChildren(...rows.map(item => el("div", {role: "listitem"}, el("button", {type: "button",
                class: "hdc-row", "aria-current": String(item.application_id === state.selected), "data-id": item.application_id,
                onclick: () => select(item.application_id), onkeydown: event => {
                    if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
                    event.preventDefault();
                    const buttons = [...listBox.querySelectorAll(".hdc-row")];
                    const i = buttons.indexOf(event.currentTarget) + (event.key === "ArrowDown" ? 1 : -1);
                    buttons[Math.max(0, Math.min(buttons.length - 1, i))].focus();
                }},
                el("span", {class: "hdc-row-top"}, el("span", {class: "hdc-row-name", text: item.name}),
                    pill(item.state_label, STATE_TONE[item.state])),
                el("span", {class: "hdc-row-meta"}, el("span", {text: item.position}),
                    el("span", {text: "ATS " + score(item.ats_score) + " · Mülakat " + score(item.interview_score)}))))));
        }

        async function load(keep = true) {
            try {
                state.data = await request("flow/decision-center");
                drawCounters();
                drawList();
                fitHeight();
                if (keep && state.selected) await select(state.selected, false);
            } catch (error) {
                listBox.replaceChildren(el("p", {class: "hdc-error", role: "alert", text: error.message}));
            }
        }

        async function select(id, focus = true) {
            state.selected = id;
            listBox.querySelectorAll(".hdc-row").forEach(b => b.setAttribute("aria-current", String(b.dataset.id === id)));
            if (state.cvUrl) { URL.revokeObjectURL(state.cvUrl); state.cvUrl = null; }
            try {
                state.detail = await request("flow/decision-center/" + encodeURIComponent(id));
            } catch (error) {
                center.replaceChildren(el("p", {class: "hdc-error", role: "alert", text: error.message}));
                right.replaceChildren();
                return;
            }
            drawCenter();
            drawRight();
            live.textContent = state.detail.application.name + " seçildi. Durum: " + state.detail.state_label + ".";
            if (focus) {
                const title = center.querySelector("h3");
                title?.focus({preventScroll: window.innerWidth > 820});
                if (window.innerWidth <= 820) right.scrollIntoView({behavior: "smooth", block: "start"});
            }
        }

        // ---------------------------------------------------------- orta panel
        function panel(id) {
            const d = state.detail;
            const r = d.report;
            if (id === "overview") {
                const last = d.decisions[d.decisions.length - 1];
                return [el("div", {class: "hdc-cards"},
                    el("div", {class: "hdc-card"}, el("h4", {text: "ATS özeti"}), d.ats
                        ? el("p", {}, atsLine(d.ats), el("br"), el("span", {class: "hdc-muted", text: d.ats.reason || "Analiz tamamlandı."}))
                        : el("p", {class: "hdc-muted", text: "ATS ön değerlendirme kaydı yok."})),
                    el("div", {class: "hdc-card"}, el("h4", {text: "Mülakat durumu"}),
                        el("p", {text: d.interview ? (d.interview.status === "completed" ? "Mülakat tamamlandı." : "Durum: " + d.interview.status)
                            : "Mülakat bağlantısı oluşturulmadı."}))),
                    el("h4", {text: "AI mülakat değerlendirmesi"}),
                    reportBlock(r, d.interview?.status),
                    el("h4", {text: "Son İK kararı"}), last
                        ? el("p", {}, el("b", {text: last.label}), " · Kullanıcı #" + last.decided_by + " · " + when(last.decided_at),
                            last.reason ? el("br") : null, last.reason || null)
                        : el("p", {class: "hdc-muted", text: "Henüz karar verilmedi."})];
            }
            if (id === "cv") {
                const doc = d.documents[d.documents.length - 1];
                if (!doc) return [el("p", {class: "hdc-muted", text: "Başvuruya bağlı CV dosyası yok."})];
                const frameBox = el("div", {class: "hdc-cv-box"}, el("p", {class: "hdc-muted", text: "CV yükleniyor…"}));
                const tools = el("div", {class: "hdc-cv-tools"}, el("span", {class: "hdc-muted",
                    text: doc.filename + " · " + Math.round(doc.size_bytes / 1024) + " KB · " + when(doc.created_at)}));
                loadCv(doc, frameBox, tools);
                return [tools, frameBox];
            }
            if (id === "ats") {
                if (!d.ats) return [el("p", {class: "hdc-muted", text: "ATS ön değerlendirme kaydı yok."})];
                return [el("p", {}, atsLine(d.ats), " · " + when(d.ats.analyzed_at)),
                    d.ats.reason ? el("p", {class: "hdc-muted", text: d.ats.reason}) : null,
                    table(["Kriter", "Ağırlık", "Sonuç", "CV'deki kanıt"], (d.ats.requirements || []).map(q =>
                        [q.requirement, String(q.weight ?? "—"), q.status, q.evidence || "—"])),
                    el("p", {class: "hdc-muted", text: "Eksik kanıt düşük yetkinlik anlamına gelmez; ATS puanı adayı otomatik elemez."})];
            }
            if (id === "report") {
                if (!r || !r.ready) return [reportBlock(r, d.interview?.status)];
                return [reportBlock(r, d.interview?.status),
                    el("details", {class: "hdc-fullreport"}, el("summary", {text: "Tam rapor metni · " + when(r.created_at)}),
                        el("pre", {class: "hdc-report", text: r.text}))];
            }
            if (id === "qa") {
                return d.answers.length ? d.answers.map(a => el("div", {class: "hdc-qa"},
                    el("div", {class: "hdc-q", text: "Soru " + a.question_number + ": " + a.question}),
                    el("div", {class: "hdc-a", text: a.answer}), el("small", {class: "hdc-muted", text: when(a.created_at)})))
                    : [el("p", {class: "hdc-muted", text: "Kayıtlı mülakat cevabı yok."})];
            }
            if (id === "history") {
                const steps = el("ol", {class: "hdc-timeline"}, d.timeline.map(step => {
                    const [label, tone] = STEP_STATE[step.state] || [step.state, "none"];
                    return el("li", {class: "hdc-step hdc-step-" + tone},
                        el("span", {class: "hdc-step-title"}, step.title, " ", pill(label, tone === "ok" ? "ok" : tone === "fail" ? "fail" : tone === "wait" ? "warn" : "muted")),
                        el("small", {text: when(step.at) + (step.source ? " · kaynak: " + step.source : "")}),
                        el("small", {text: step.detail}));
                }));
                const extra = [];
                if (d.case_id) extra.push(el("button", {type: "button", class: "hdc-btn", onclick: async e => {
                    e.currentTarget.disabled = true;
                    const box = e.currentTarget.nextElementSibling;
                    try {
                        const evidence = await request("selin/cases/" + encodeURIComponent(d.case_id) + "/evidence");
                        box.replaceChildren(list(evidence.steps.map(s => (s.title || s.code) + " · " + s.state + (s.detail ? " · " + s.detail : ""))));
                    } catch (error) { box.textContent = error.message; }
                }}, "FLOW işlem kanıtlarını göster"), el("div"));
                return [el("p", {class: "hdc-muted", text: "Her adım yalnızca kaynak kaydı varsa “Gerçekleşti” görünür. İK'nın girdiği takvim notu aday seçimi sayılmaz."}),
                    steps, ...extra,
                    el("h4", {text: "ATS aşama geçişleri"}), table(["Önceki", "Sonraki", "Kapanış", "Kullanıcı", "Zaman"],
                        d.stage_events.map(s => [STAGES[s.previous] || s.previous, STAGES[s.next] || s.next, s.closure || "—", "#" + s.user_id, when(s.occurred_at)])),
                    el("h4", {text: "Görüşme takvimi"}), table(["Başlangıç", "Tür", "Durum", "Not"],
                        d.schedules.map(s => [when(s.starts_at), {video: "Görüntülü", onsite: "Yüz yüze", phone: "Telefon"}[s.kind] || s.kind,
                            {planned: "Planlı", completed: "Tamamlandı", cancelled: "İptal"}[s.status] || s.status, s.notes || "—"]))];
            }
            if (id === "training") {
                if (!d.trainings.length) return [el("p", {class: "hdc-muted", text: "Bu başvuru için Karar Merkezi'nden atanmış eğitim yok."})];
                return d.trainings.map((t, index) => {
                    const pct = t.total_lessons ? Math.round(100 * t.completed_lessons / t.total_lessons) : 0;
                    const link = state.trainingLinks[state.selected];
                    return el("div", {class: "hdc-card"},
                        el("h4", {text: "ERNASA Akademi · " + t.program_id}),
                        el("div", {class: "hdc-progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
                            "aria-valuenow": String(pct), "aria-label": "Eğitim ilerlemesi"}, el("div", {style: "width:" + pct + "%"})),
                        el("p", {text: t.completed_lessons + "/" + t.total_lessons + " ders tamamlandı (%" + pct + ")"}),
                        table(["Ders", "Durum"], t.modules.map(m => [m.title, m.done ? "Tamamlandı" : "Tamamlanmadı"])),
                        el("p", {class: "hdc-muted", text: "Atama: " + when(t.assigned_at) + " · Bağlantı açılışı: " + when(t.opened_at) +
                            " · Başlangıç: " + when(t.started_at) + " · Son etkinlik: " + when(t.last_activity_at) + " · Son geçerlilik: " + when(t.expires_at)}),
                        el("p", {class: "hdc-muted", text: t.notice}),
                        index === d.trainings.length - 1 && link ? sendTrainingButton(link) : null);
                });
            }
            if (id === "notifications") {
                const rows = d.timeline.filter(s => ["availability_email", "interview_email"].includes(s.code));
                const notices = d.candidate_notices || [];
                const reviews = (d.reviews || []).map(r => el("li", {text: r.reason_code + " · " + when(r.created_at)}));
                return [el("p", {class: "hdc-muted", text: "“Sağlayıcı kabul etti”, e-posta servisinin isteği aldığını gösterir; alıcıya teslim ayrıca kanıtlanmaz. “Hazırlandı” durumundaki ileti henüz gönderilmemiştir."}),
                    el("h4", {text: "Davet ve mülakat e-postaları"}),
                    table(["Bildirim", "Durum", "Zaman", "Açıklama"], rows.map(s => [s.title, (STEP_STATE[s.state] || [s.state])[0], when(s.at), s.detail])),
                    el("h4", {text: "Aday bilgilendirmeleri" + (d.automation ? " (otomatik akış)" : "")}),
                    notices.length ? table(["Bildirim", "Durum", "Deneme", "Son güncelleme"],
                        notices.map(n => [n.title, n.status_label, String(n.attempts), when(n.updated_at)]))
                        : el("p", {class: "hdc-muted", text: "Bu aday için bilgilendirme kaydı yok."}),
                    reviews.length ? el("div", {class: "hdc-card"}, el("h4", {text: "İnceleme gerektiren durumlar"}), el("ul", {}, ...reviews)) : null,
                    (d.requests || []).length ? el("div", {class: "hdc-card"}, el("h4", {text: "Aday talepleri"}),
                        el("ul", {}, ...d.requests.map(r => el("li", {text: r.kind + " · " + (r.status === "open" ? "açık" : "kapalı") + " · " + when(r.created_at) + (r.message ? " · " + r.message : "")})))) : null,
                    el("p", {class: "hdc-muted", text: d.notice})].filter(Boolean);
            }
            return [];
        }

        function sendTrainingButton(link) {
            const token = link.split("/egitim/")[1];
            return el("div", {class: "hdc-sendbox"},
                el("p", {text: "Eğitim daveti henüz gönderilmedi. Gönderim mevcut Eğitim Atama servisiyle, onayınızla yapılır."}),
                el("button", {type: "button", class: "hdc-btn hdc-training", onclick: async e => {
                    if (!window.confirm("Eğitim daveti adayın e-posta adresine gönderilsin mi?")) return;
                    e.currentTarget.disabled = true;
                    try {
                        const result = await window.portalJson("/api/egitim/ik/davet/" + encodeURIComponent(token) + "/gonder",
                            {method: "POST"}, "Eğitim daveti gönderilemedi.");
                        delete state.trainingLinks[state.selected];
                        e.currentTarget.replaceWith(el("p", {role: "status", text: result.message}));
                    } catch (error) { e.currentTarget.disabled = false; window.alert(error.message); }
                }}, "Eğitim davetini gönder"));
        }

        async function loadCv(doc, box, tools) {
            try {
                const response = await fetch(API + "ats/applications/" + encodeURIComponent(state.selected) +
                    "/documents/" + encodeURIComponent(doc.id), {cache: "no-store"});
                if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "CV açılamadı.");
                const blob = await response.blob();
                if (state.cvUrl) URL.revokeObjectURL(state.cvUrl);
                state.cvUrl = URL.createObjectURL(blob);
                if (doc.media_type === "application/pdf") {
                    const frame = el("iframe", {class: "hdc-cv-frame", src: state.cvUrl, title: "Orijinal CV: " + doc.filename});
                    box.replaceChildren(frame);
                    tools.append(el("button", {type: "button", class: "hdc-btn", onclick: () =>
                        frame.requestFullscreen ? frame.requestFullscreen().catch(() => window.open(state.cvUrl, "_blank", "noopener"))
                            : window.open(state.cvUrl, "_blank", "noopener")}, "Tam ekran"),
                        el("a", {class: "hdc-btn", href: state.cvUrl, target: "_blank", rel: "noopener"}, "Yeni sekmede aç"));
                } else {
                    box.replaceChildren(el("p", {text: "Word belgeleri tarayıcıda önizlenemez."}),
                        el("a", {class: "hdc-btn", href: state.cvUrl, download: doc.filename}, "Dosyayı indir"));
                }
            } catch (error) {
                box.replaceChildren(el("p", {class: "hdc-error", role: "alert", text: error.message}));
            }
        }

        function drawCenter() {
            const d = state.detail;
            const a = d.application;
            const tabs = el("div", {class: "hdc-tabs", role: "tablist", "aria-label": "Aday değerlendirme bölümleri"});
            const panels = el("div", {class: "hdc-tabpanels"});
            for (const [id, label] of TABS) {
                const selected = id === state.tab;
                tabs.append(el("button", {type: "button", role: "tab", class: "hdc-tab", id: "hdc-tab-" + id,
                    "aria-controls": "hdc-panel-" + id, "aria-selected": String(selected), tabindex: selected ? "0" : "-1",
                    "data-tab": id}, label));
                // Tüm sekmeler bir kez oluşturulur; geçişte yalnızca görünürlük değişir (veri yeniden üretilmez).
                panels.append(el("section", {class: "hdc-tabpanel", role: "tabpanel", id: "hdc-panel-" + id,
                    "aria-labelledby": "hdc-tab-" + id, tabindex: "0", hidden: !selected}, panel(id)));
            }
            tabs.addEventListener("click", e => { const t = e.target.closest("[role=tab]"); if (t) activate(t.dataset.tab); });
            tabs.addEventListener("keydown", e => {
                const ids = TABS.map(t => t[0]);
                let i = ids.indexOf(e.target.dataset?.tab);
                if (i < 0) return;
                if (e.key === "ArrowRight") i = (i + 1) % ids.length;
                else if (e.key === "ArrowLeft") i = (i - 1 + ids.length) % ids.length;
                else if (e.key === "Home") i = 0;
                else if (e.key === "End") i = ids.length - 1;
                else return;
                e.preventDefault();
                activate(ids[i], true);
            });
            const flash = state.flash ? el("p", {class: "hdc-flash", role: "status", text: state.flash}) : null;
            state.flash = null;
            center.replaceChildren(
                el("header", {class: "hdc-cand"},
                    el("div", {class: "hdc-cand-id"},
                        el("h3", {tabindex: "-1", text: a.name}),
                        el("p", {class: "hdc-muted", text: a.position + " · " + (a.email || "e-posta yok") + (a.phone ? " · " + a.phone : "")}),
                        el("p", {}, pill(d.state_label, STATE_TONE[d.state]), " ",
                            el("span", {class: "hdc-muted", text: "ATS aşaması: " + (STAGES[a.stage] || a.stage) + (d.closure_label ? " · " + d.closure_label : "")}))),
                    el("div", {class: "hdc-scores"},
                        el("div", {class: "hdc-score"}, el("small", {text: "ATS uygunluk"}), el("b", {text: score(d.scores.ats)})),
                        el("div", {class: "hdc-score"}, el("small", {text: "AI mülakat"}), el("b", {text: score(d.scores.interview)})))),
                ...[el("p", {class: "hdc-note", text: d.scores.note}), flash, tabs, panels].filter(Boolean));
        }

        function activate(id, focus = false) {
            state.tab = id;
            center.querySelectorAll("[role=tab]").forEach(t => {
                const on = t.dataset.tab === id;
                t.setAttribute("aria-selected", String(on));
                t.tabIndex = on ? 0 : -1;
                if (on && focus) t.focus();
            });
            center.querySelectorAll("[role=tabpanel]").forEach(p => { p.hidden = p.id !== "hdc-panel-" + id; });
            const box = center.querySelector(".hdc-tabpanels");
            if (box) box.scrollTop = 0;
        }

        // ---------------------------------------------------------- sağ panel
        function drawRight() {
            const d = state.detail;
            const help = el("p", {class: "hdc-muted", id: "hdc-decision-help",
                text: d.can_decide ? "Karar sizin adınıza ve şu anki zamanla kaydedilir." : d.decision_help});
            right.replaceChildren(el("div", {class: "hdc-aside"},
                el("section", {}, el("h4", {text: "Güncel aşama"}), el("div", {class: "hdc-stagebox"},
                    pill(d.state_label, STATE_TONE[d.state]),
                    el("p", {class: "hdc-muted", text: "ATS: " + (STAGES[d.application.stage] || d.application.stage) +
                        (d.closure_label ? " · " + d.closure_label : "")}))),
                el("section", {}, el("h4", {id: "hdc-decision-title", text: "İK kararı"}),
                    el("div", {class: "hdc-actions", role: "group", "aria-labelledby": "hdc-decision-title"},
                        DECISIONS.map(([key, label, cls]) => el("button", {type: "button", class: "hdc-btn " + cls,
                            disabled: !d.can_decide, "aria-describedby": "hdc-decision-help", onclick: () => openDecision(key)}, label))),
                    help),
                el("section", {class: "hdc-aside-history"}, el("h4", {text: "Karar geçmişi"}),
                    d.decisions.length ? el("ul", {class: "hdc-mini"}, d.decisions.slice().reverse().map(x =>
                        el("li", {}, el("b", {text: x.label}), el("br"), el("span", {class: "hdc-muted",
                            text: "Kullanıcı #" + x.decided_by + " · " + when(x.decided_at)})))) : el("p", {class: "hdc-muted", text: "Henüz karar yok."})),
                el("p", {class: "hdc-muted hdc-small", text: d.notice})));
        }

        // ---------------------------------------------------------- karar penceresi
        function dialog(title, body, submitLabel, submitClass, run) {
            const previous = document.activeElement;
            const error = el("p", {class: "hdc-error", role: "alert", hidden: true});
            const submit = el("button", {type: "submit", class: "hdc-btn " + submitClass}, submitLabel);
            const close = () => { back.remove(); previous?.focus?.(); };
            const form = el("form", {class: "hdc-dialog-form"}, el("div", {class: "hdc-dialog-body"}, error, body),
                el("footer", {}, el("button", {type: "button", class: "hdc-btn", onclick: close}, "Vazgeç"), submit));
            const box = el("div", {class: "hdc-dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": "hdc-dialog-title"},
                el("header", {}, el("h3", {id: "hdc-dialog-title", text: title}),
                    el("button", {type: "button", class: "hdc-x", "aria-label": "Kapat", onclick: close}, "×")), form);
            const back = el("div", {class: "hdc-backdrop", onclick: e => { if (e.target === back) close(); }}, box);
            back.addEventListener("keydown", e => {
                if (e.key === "Escape") { e.preventDefault(); close(); }
                if (e.key === "Tab") {
                    const items = [...box.querySelectorAll("button,input,select,textarea,a[href]")].filter(x => !x.disabled);
                    if (e.shiftKey && document.activeElement === items[0]) { e.preventDefault(); items.at(-1).focus(); }
                    else if (!e.shiftKey && document.activeElement === items.at(-1)) { e.preventDefault(); items[0].focus(); }
                }
            });
            form.addEventListener("submit", async e => {
                e.preventDefault();
                if (!form.checkValidity()) { form.reportValidity(); return; }
                submit.disabled = true;  // çift tıklamaya karşı
                try { await run(new FormData(form)); close(); }
                catch (err) { error.hidden = false; error.textContent = err.message; submit.disabled = false; }
            });
            document.body.append(back);
            (box.querySelector("textarea,select,input") || submit).focus();
        }
        const label = (text, control, hint) => el("label", {class: "hdc-field"}, el("span", {text}), control,
            hint ? el("small", {class: "hdc-muted", text: hint}) : null);

        async function openDecision(kind) {
            const d = state.detail;
            const intro = el("p", {class: "hdc-intro"}, el("b", {text: d.application.name}), " · " + d.application.position,
                el("br"), "Karar sizin adınıza ve şu anki zamanla kaydedilir. Adaya ilgili bilgilendirme e-postası kuyruğa alınır.");
            const base = {decision: kind, expected_revision: d.application.revision,
                expected_last_decision_id: d.last_decision_id, idempotency_key: newKey()};
            const send = async extra => {
                const result = await post("flow/decision-center/" + encodeURIComponent(state.selected) + "/decision", {...base, ...extra});
                if (result.training_link) state.trainingLinks[state.selected] = result.training_link;
                state.flash = "Karar kaydedildi: " + result.decision.label + " · " + when(result.decision.decided_at) + ". " + (result.notice || "");
                await load(true);
            };
            const reason = (required, hint) => label(required ? "Gerekçe (zorunlu)" : "Karar notu (isteğe bağlı)",
                el("textarea", {name: "reason", rows: "3", maxlength: "2000", required: required || null, minlength: required ? "3" : null}), hint);
            if (kind === "approve") {
                dialog("Adayı onayla", el("div", {}, intro, reason(false),
                    el("p", {class: "hdc-muted", text: "Başvuru ATS'de “Teklif” aşamasına alınır. Teklif, mevcut Teklifler ekranında hazırlanır."})),
                    "Onayla", "hdc-approve", f => send({reason: f.get("reason")}));
            } else if (kind === "reject") {
                dialog("Adayı reddet", el("div", {}, intro,
                    reason(true, "Gerekçe aday dosyasının ATS notlarına kaydedilir. ATS veya AI puanı tek başına gerekçe olmamalıdır."),
                    el("p", {class: "hdc-muted", text: "Başvuru “Olumsuz” olarak kapatılır. Adaya nazik bir sonuç e-postası gider; gerekçe adaya iletilmez, yalnızca dosyada saklanır."})),
                    "Reddet", "hdc-reject", f => send({reason: f.get("reason")}));
            } else if (kind === "second_interview") {
                if (!state.assignees) state.assignees = await request("assignees").catch(() => ({users: []}));
                const interviewer = el("select", {name: "interviewer_id"}, el("option", {value: "", text: "Takvimi sonra planla"}),
                    (state.assignees.users || []).map(u => el("option", {value: String(u.id), text: u.name})));
                dialog("İkinci görüşme", el("div", {}, intro, reason(false),
                    el("fieldset", {class: "hdc-fieldset"}, el("legend", {text: "Görüşme takvimi (isteğe bağlı)"}),
                        label("Görüşmeci", interviewer),
                        label("Başlangıç (Türkiye saati)", el("input", {type: "datetime-local", name: "starts_at"})),
                        label("Süre", el("select", {name: "minutes"}, ["30", "45", "60", "90"].map(m => el("option", {value: m, text: m + " dakika", selected: m === "45" || null})))),
                        label("Görüşme türü", el("select", {name: "kind"}, el("option", {value: "video", text: "Görüntülü"}),
                            el("option", {value: "onsite", text: "Yüz yüze"}), el("option", {value: "phone", text: "Telefon"})))),
                    el("p", {class: "hdc-muted", text: "Takvim kaydı mevcut Görüşme Takvimi'ne İK tarafından girilir ve “aday saat seçti” olarak gösterilmez."})),
                    "İkinci görüşmeye al", "hdc-second", f => {
                        let schedule = null;
                        if (f.get("interviewer_id") || f.get("starts_at")) {
                            if (!f.get("interviewer_id") || !f.get("starts_at")) throw new Error("Takvim için görüşmeci ve başlangıç birlikte seçilmelidir.");
                            const start = f.get("starts_at");
                            const end = new Date(new Date(start + ":00").getTime() + Number(f.get("minutes")) * 60000);
                            const pad = n => String(n).padStart(2, "0");
                            const local = end.getFullYear() + "-" + pad(end.getMonth() + 1) + "-" + pad(end.getDate()) + "T" + pad(end.getHours()) + ":" + pad(end.getMinutes());
                            schedule = {interviewer_id: Number(f.get("interviewer_id")), starts_at: start, ends_at: local, kind: f.get("kind")};
                        }
                        return send({reason: f.get("reason"), schedule});
                    });
            } else if (kind === "assign_training") {
                if (!state.catalog) state.catalog = await window.portalJson("/api/egitim/ik/katalog", {cache: "no-store"}, "Eğitim kataloğu alınamadı.");
                const programs = state.catalog.programs || [];
                const titles = Object.fromEntries((state.catalog.modules || []).map(m => [m.id, m.title]));
                const program = el("select", {name: "program_id", required: true}, programs.map(p => el("option", {value: p.id, text: p.title})));
                const modules = el("fieldset", {class: "hdc-fieldset"}, el("legend", {text: "Dersler"}));
                const drawModules = () => {
                    const chosen = programs.find(p => p.id === program.value) || programs[0];
                    modules.replaceChildren(el("legend", {text: "Dersler"}), ...(chosen?.allowed_modules || []).map(id =>
                        el("label", {class: "hdc-check"}, el("input", {type: "checkbox", name: "module_ids", value: id,
                            checked: (chosen.default_modules || []).includes(id) || null}), titles[id] || id)));
                };
                program.addEventListener("change", drawModules);
                drawModules();
                dialog("Eğitim ata", el("div", {}, intro, label("Eğitim programı (ERNASA Akademi)", program), modules,
                    label("Süre (gün)", el("input", {type: "number", name: "days", min: "1", max: "30", value: "14", required: true})),
                    reason(false),
                    el("p", {class: "hdc-muted", text: "Atama mevcut Akademi V9 kayıtlarına yazılır. Davet e-postası otomatik gitmez; “Eğitim ve İlerleme” sekmesinden onayınızla gönderilir."})),
                    "Eğitimi ata", "hdc-training", f => {
                        const ids = f.getAll("module_ids");
                        if (!ids.length) throw new Error("En az bir ders seçin.");
                        return send({reason: f.get("reason"), training: {program_id: f.get("program_id"), module_ids: ids, days: Number(f.get("days"))}});
                    });
            }
        }

        load(false);
        return {reload: () => load(true)};
    }

    // ---------------------------------------------------------- otomatik süreç ayarı (pozisyon bazında)
    async function json(path, options = {}) {
        if (typeof window.portalJson === "function")
            return window.portalJson(path, {cache: "no-store", ...options}, "İşlem tamamlanamadı.");
        const response = await fetch(path, {cache: "no-store", ...options});
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "İşlem tamamlanamadı.");
        return body;
    }
    const DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"];

    async function mountAutomation(root) {
        root.replaceChildren(el("p", {class: "hdc-muted", text: "Ayarlar yükleniyor…"}));
        const {positions = []} = await json("/api/ik/positions");
        const select = el("select", {id: "hdc-auto-position", "aria-label": "Pozisyon"},
            ...positions.map(p => el("option", {value: p.id, text: p.name})));
        const form = el("form", {class: "hdc-autoform"});
        const status = el("p", {role: "status", class: "hdc-muted"});
        async function load() {
            const cfg = await json("/api/ik/flow/positions/" + encodeURIComponent(select.value) + "/automation");
            const input = (name, value, attrs = {}) => el("input", {name, value: String(value), ...attrs});
            form.replaceChildren(
                el("label", {class: "hdc-check"}, el("input", {type: "checkbox", name: "enabled", ...(cfg.enabled ? {checked: ""} : {})}),
                    " Bu pozisyonda başvurudan mülakat raporuna kadar süreci otomatik yürüt (aday bazında İK onayı istenmez)"),
                el("fieldset", {}, el("legend", {text: "Adayların seçebileceği günler"}),
                    ...DAYS.map((d, i) => el("label", {class: "hdc-day"}, el("input", {type: "checkbox", name: "day", value: String(i + 1),
                        ...(cfg.slot_weekdays.includes(i + 1) ? {checked: ""} : {})}), " " + d))),
                el("div", {class: "hdc-autogrid"},
                    el("label", {}, "Başlangıç saati", input("slot_start", cfg.slot_start, {type: "time"})),
                    el("label", {}, "Bitiş saati", input("slot_end", cfg.slot_end, {type: "time"})),
                    el("label", {}, "Aralık (dk)", input("slot_minutes", cfg.slot_minutes, {type: "number", min: "15", max: "240"})),
                    el("label", {}, "Adaya durum bilgisi (gün)", input("notice_days", cfg.notice_days, {type: "number", min: "1", max: "90"})),
                    el("label", {}, "İK hatırlatması (gün)", input("hr_reminder_days", cfg.hr_reminder_days, {type: "number", min: "1", max: "90"}))),
                el("p", {class: "hdc-muted", text: "Mülakat bağlantısı adayın seçtiği saatte açılır ve 8 saat geçerlidir. Yapay zekâ işe alım veya ret kararı vermez; nihai karar İK Karar Merkezi'ndedir."}),
                el("button", {type: "submit", class: "hdc-btn hdc-approve", text: "Ayarları kaydet"}));
        }
        form.addEventListener("submit", async event => {
            event.preventDefault();
            const data = new FormData(form);
            const button = form.querySelector('button[type="submit"]');
            const enabled = data.get("enabled") === "on";
            if (button) { button.disabled = true; button.textContent = "Kaydediliyor…"; }
            status.textContent = "Ayarlar sunucuya kaydediliyor…";
            status.setAttribute("aria-live", "polite");
            try {
                const result = await json("/api/ik/flow/positions/" + encodeURIComponent(select.value) + "/automation", {
                    method: "PUT", headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({enabled, slot_weekdays: data.getAll("day").map(Number),
                        slot_start: data.get("slot_start"), slot_end: data.get("slot_end"),
                        slot_minutes: Number(data.get("slot_minutes")), notice_days: Number(data.get("notice_days")),
                        hr_reminder_days: Number(data.get("hr_reminder_days"))})});
                const saved = !!result.enabled;
                status.textContent = "✓ Ayarlar kaydedildi. Otomatik akış: " + (saved ? "AÇIK" : "KAPALI") + ".";
                status.style.color = "#08765c";
                status.style.fontWeight = "700";
                if (button) button.textContent = "✓ Kaydedildi — " + (saved ? "AÇIK" : "KAPALI");
            } catch (error) {
                status.textContent = "Kaydedilemedi: " + error.message;
                status.style.color = "#ad3434";
                status.style.fontWeight = "700";
                if (button) button.textContent = "Tekrar dene";
            } finally {
                if (button) button.disabled = false;
                status.scrollIntoView({block:"nearest", behavior:"smooth"});
            }
        });
        select.addEventListener("change", () => load().catch(error => { status.textContent = error.message; }));
        root.replaceChildren(el("label", {for: "hdc-auto-position", text: "Pozisyon"}), select, form, status);
        if (positions.length) await load(); else status.textContent = "Önce bir pozisyon oluşturun.";
    }

    async function mountReports(root) {
        const r = await json("/api/ik/flow/reports");
        const kv = (label, value) => el("div", {class: "hdc-kpi"}, el("b", {text: String(value ?? "—")}), el("span", {text: label}));
        const labels = {prepared: "Hazırlandı", provider_accepted: "Sağlayıcı kabul etti", delivered: "Teslim edildi",
            retry: "Yeniden denenecek", failed: "Başarısız", unconfirmed: "Doğrulanamadı", cancelled: "İptal", sending: "Gönderiliyor"};
        root.replaceChildren(
            el("div", {class: "hdc-kpis"}, kv("Başvuru", r.applications), kv("Tamamlanan mülakat", r.interviews_completed),
                kv("İK kararı bekleyen", r.states.awaiting_hr), kv("Ort. İK bekleme (gün)", r.average_hr_wait_days),
                kv("Onaylanan", r.states.approved), kv("Reddedilen", r.states.rejected)),
            el("h4", {text: "Aday bildirimleri"}),
            el("ul", {}, ...Object.entries(r.notices).map(([k, v]) => el("li", {text: (labels[k] || k) + ": " + v}))),
            el("p", {class: "hdc-muted", text: r.note}),
            el("h4", {text: "Süresi aşılan İK kararları"}),
            r.hr_overdue.length ? el("ul", {}, ...r.hr_overdue.map(o => el("li", {text: o.name + " · " + o.days + " gün"})))
                : el("p", {class: "hdc-muted", text: "Hatırlatma süresini aşan aday yok."}),
            el("h4", {text: "İnceleme kuyruğu (teknik istisnalar)"}),
            r.reviews.length ? el("ul", {}, ...r.reviews.map(v => el("li", {text: v.label + " · " + when(v.created_at)})))
                : el("p", {class: "hdc-muted", text: "İnceleme bekleyen istisna yok."}),
            el("h4", {text: "Aday talepleri (itiraz, düzeltme, teknik sorun)"}),
            r.requests.length ? el("ul", {}, ...r.requests.map(q => el("li", {text: q.label + " · " + when(q.created_at) + (q.message ? " · " + q.message : "")})))
                : el("p", {class: "hdc-muted", text: "Açık talep yok. KVKK silme/erişim talepleri ATS Gizlilik ekranında listelenir."}));
    }

    window.ErnasaDecisionCenter = {mount, mountAutomation, mountReports};
})();
