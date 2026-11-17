"use strict";

function buildInterviewShare(invite) {
    const expiry = new Intl.DateTimeFormat("tr-TR", {
        dateStyle:"long",timeStyle:"medium",timeZone:"Europe/Istanbul"
    }).format(new Date(invite.expires_at)) + " (Türkiye saati)";
    const subject = invite.company + " · " + invite.position + " mülakat daveti";
    const message = "Merhaba " + invite.name + ",\n\n" + invite.company + " bünyesindeki " +
        invite.position + " pozisyonu için mülakat bağlantınız: " + invite.interview_url +
        "\n\nSon geçerlilik: " + expiry + "\n\nİyi çalışmalar.";
    const phone = invite.phone || "";
    let number = phone.replace(/\D/g,"");
    if (number.startsWith("00")) number = number.slice(2);
    else if (!phone.startsWith("+") && number.length === 11 && number.startsWith("0")) number = "90" + number.slice(1);
    else if (!phone.startsWith("+") && number.length === 10) number = "90" + number;
    const whatsapp = /^[1-9]\d{7,14}$/.test(number) ?
        "https://wa.me/" + number + "?text=" + encodeURIComponent(message) : "";
    return {expiry,message,whatsapp};
}

(() => {
    const byId = id => document.getElementById(id);
    const upload = byId("cvFile"), uploadArea = upload.closest("label");
    const fileStatus = byId("fileStatus"), status = byId("analysisBox");
    const analyze = byId("analyzeButton"), create = byId("createLinkButton");
    const position = byId("portalPosition"), positionName = byId("positionName");
    const candidateName = byId("candidateName"), phone = byId("candidatePhone"), email = byId("candidateEmail");
    const company = byId("companyName"), actions = byId("interviewActions");
    const link = byId("interviewLink"), copy = byId("copyLinkButton");
    const actionStatus = byId("inviteShareStatus");
    const linkStatus = {waiting:"Bekliyor",created:"Bekliyor",opened:"Bağlantı açıldı",
        started:"Mülakat başladı",completed:"Tamamlandı",expired:"Süresi doldu"};
    let file, requestId, legacyCvText, analyzed = false, busy = false, issued = false;

    function setBusy(value) {
        busy = value;
        upload.disabled = busy;
        analyze.disabled = busy || !file || issued;
        create.disabled = busy || !analyzed || (issued && companyPortalPage);
    }

    function selectFile(selected) {
        if (busy || !selected) return;
        file = null;
        requestId = legacyCvText = "";
        analyzed = issued = false;
        candidateName.value = phone.value = email.value = "";
        actions.style.display = "none";
        actionStatus.textContent = "";
        if (!/\.(pdf|docx)$/i.test(selected.name) || !selected.size || selected.size > 10*1024*1024) {
            upload.value = "";
            fileStatus.textContent = "En fazla 10 MB boyutunda, boş olmayan bir PDF veya DOCX seçin.";
            fileStatus.classList.remove("file-success");
            fileStatus.classList.add("file-error");
            status.textContent = "";
        } else {
            file = selected;
            fileStatus.classList.remove("file-error");
            fileStatus.classList.add("file-success");
            fileStatus.textContent = selected.name;
            status.textContent = "İletişim bilgilerini çıkarmak için CV Analiz Et’e basın.";
        }
        setBusy(false);
    }

    upload.addEventListener("change", () => selectFile(upload.files[0]));
    for (const eventName of ["dragenter","dragover","dragleave","drop"]) {
        uploadArea.addEventListener(eventName, event => {
            event.preventDefault();
            uploadArea.classList.toggle("drag-active",eventName === "dragenter" || eventName === "dragover");
            if (eventName === "drop") selectFile(event.dataTransfer.files[0]);
        });
    }
    window.addEventListener("dragover", event => event.preventDefault());
    window.addEventListener("drop", event => event.preventDefault());

    function updatePositionInput() {
        positionName.style.display = position.value === "new" ? "" : "none";
    }

    if (companyPortalPage) {
        position.style.removeProperty("display");
        updatePositionInput();
        position.addEventListener("change", updatePositionInput);
        document.addEventListener("ik-portal-screen", async event => {
            if (event.detail !== "cv") return;
            const selected = position.value;
            try {
                const metadata = await portalMetadataPromise;
                const {positions} = await portalJson("/api/ik/ai/cv/positions", {}, "Pozisyonlar alınamadı.");
                position.replaceChildren(new Option("Açık pozisyon seçin",""));
                for (const item of positions) position.add(new Option(item.name,item.id));
                if (!metadata?.products.includes("ats")) position.add(new Option("Yeni pozisyon","new"));
                if ([...position.options].some(option => option.value === selected)) position.value = selected;
                updatePositionInput();
            } catch (error) { status.textContent = error.message; }
        });
    }

    analyze.addEventListener("click", async () => {
        if (busy || !file || issued) return;
        setBusy(true);
        status.textContent = "CV okunuyor…";
        const data = new FormData();
        data.append("cv",file);
        try {
            const path = companyPortalPage ? "/api/ik/ai/cv/preview" : "/api/cv/upload";
            const result = await portalJson(path,{method:"POST",body:data},"CV okunamadı.");
            const contact = companyPortalPage ? result : result.contact || {};
            requestId ||= result.import_id;
            legacyCvText = result.text || "";
            candidateName.value = contact.name || "";
            phone.value = contact.phone || "";
            email.value = contact.email || "";
            const missing = [[candidateName,"ad soyad"],[phone,"telefon"],[email,"e-posta"]]
                .filter(([field]) => !field.value).map(([,label]) => label);
            status.textContent = missing.length ? "CV’de bulunamadı · " + missing.join(", ") : "İletişim bilgileri çıkarıldı.";
            analyzed = true;
        } catch (error) {
            status.textContent = error.message;
        } finally { setBusy(false); }
    });

    create.addEventListener("click", async () => {
        if (!analyzed || busy || (issued && companyPortalPage)) return;
        const candidate = {name:candidateName.value.trim(),phone:phone.value.trim(),email:email.value.trim()};
        if (!candidate.name || !email.checkValidity()) {
            status.textContent = "Adayın ad soyadını ve varsa geçerli e-posta adresini girin.";
            return;
        }
        const namedPosition = positionName.value.trim();
        if (companyPortalPage ? !position.value || (position.value === "new" && !namedPosition) : !namedPosition) {
            status.textContent = "Pozisyon bilgisi gerekli.";
            return;
        }
        if (!companyPortalPage && (!company.value.trim() || namedPosition.length < 3)) {
            status.textContent = "Firma ve pozisyon bilgilerini kontrol edin.";
            return;
        }
        setBusy(true);
        status.textContent = "Mülakat bağlantısı oluşturuluyor…";
        try {
            let result;
            if (companyPortalPage) {
                const body = new FormData();
                body.append("cv",file);
                body.append("selection",JSON.stringify({...candidate,import_id:requestId,
                    position_id:position.value === "new" ? "" : position.value,
                    new_position:position.value === "new" ? namedPosition : ""}));
                result = await portalJson("/api/ik/ai/cv/invite",{method:"POST",body},"Mülakat bağlantısı oluşturulamadı.");
            } else {
                const fields = {...candidate,company:company.value.trim(),position:namedPosition,cv_text:legacyCvText};
                result = {...fields,...await portalJson("/api/interview-links",{method:"POST",
                    headers:{"Content-Type":"application/json"},body:JSON.stringify(fields)},"Mülakat bağlantısı oluşturulamadı.")};
            }
            showInvite(result);
            issued = true;
            status.textContent = "Mülakat bağlantısı oluşturuldu.";
        } catch (error) { status.textContent = error.message; }
        finally { setBusy(false); }
    });

    function shareAction(id, url, reason) {
        const action = byId(id);
        const feedback = byId(id + "Reason");
        if (url) action.href = url;
        else action.removeAttribute("href");
        action.setAttribute("aria-disabled",String(!url));
        feedback.textContent = url ? "" : reason;
    }

    function showInvite(invite) {
        const {expiry,whatsapp} = buildInterviewShare(invite);
        const deadline = byId("inviteExpiry");
        deadline.dateTime = invite.expires_at;
        deadline.textContent = expiry;
        byId("candidateNumber").textContent = invite.candidate_number;
        byId("candidateStatus").textContent = linkStatus[invite.status] || invite.status;
        link.value = invite.interview_url;
        shareAction("whatsappButton",whatsapp,
            invite.phone ? "WhatsApp için geçerli bir aday telefonu gerekli." : "Adayın telefonu eksik.");
        const emailAction = byId("emailButton");
        const emailFeedback = byId("emailButtonReason");
        emailAction.disabled = !invite.email || !invite.interview_id;
        emailFeedback.textContent = emailAction.disabled ? "Gönderim için kayıtlı aday e-postası gerekli." : "";
        emailAction.onclick = async () => {
            if (emailAction.disabled) return;
            emailAction.disabled = true;
            actionStatus.textContent = "Kurumsal e-posta gönderiliyor…";
            try {
                const result = await portalJson(
                    "/api/ik/interviews/" + encodeURIComponent(invite.interview_id) + "/send-email",
                    {method:"POST"}, "Kurumsal e-posta gönderilemedi.");
                actionStatus.textContent = result.message || "Kurumsal e-posta gönderildi.";
            } catch (error) {
                actionStatus.textContent = error.message;
                emailAction.disabled = false;
            }
        };
        actions.style.display = "block";
    }

    copy.addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(link.value);
            actionStatus.textContent = "Bağlantı kopyalandı.";
        } catch {
            link.focus();
            link.select();
            actionStatus.textContent = "Bağlantıyı kopyalamak için Ctrl+C veya ⌘C kullanın.";
        }
    });
})();
