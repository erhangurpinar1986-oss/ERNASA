"use strict";
// Firma bazlı eğitim: birincil işlevler içerik alanında, tek sol menü başlığı.
if (companyPortalPage) {
    const root=document.getElementById("trainingScreen");
    const byId=id=>document.getElementById(id);
    const endpoint="/api/ik/training/";
    let data={courses:[],enrollments:[],employees:[],completed_interviews:[]};
    let draftLessons=[];

    function element(tag,value="",cls="") {
        const node=document.createElement(tag);
        node.textContent=value;
        if(cls)node.className=cls;
        return node;
    }
    function label(text,field) {
        const l=element("label",text);l.append(field);return l;
    }
    function control(name,kind="text",placeholder="") {
        const input=document.createElement(kind==="textarea"?"textarea":"input");
        if(kind!=="textarea")input.type=kind;
        input.name=name;input.placeholder=placeholder;return input;
    }
    function status(message,error=false) {
        const x=byId("trainingStatus");
        x.textContent=message;x.classList.toggle("saas-error",error);
    }
    async function api(route,method="GET",payload) {
        const options={method,cache:"no-store"};
        if(payload!==undefined){options.headers={"Content-Type":"application/json"};options.body=JSON.stringify(payload)}
        return portalJson(endpoint+route,options,"Eğitim işlemi başarısız.");
    }
    function picker(name,items,placeholder) {
        const sel=document.createElement("select");sel.name=name;
        sel.append(new Option(placeholder,""));
        for(const [value,text] of items)sel.append(new Option(text,value));
        return sel;
    }
    function showCourses() {
        const result=byId("trainingCourseList");
        result.replaceChildren();
        if(!data.courses.length){result.append(element("p","Henüz eğitim programı yok. Aşağıdaki formdan ilk programı oluşturabilirsiniz."));return}
        for(const item of data.courses) {
            const card=element("div","","saas-panel");
            const title=element("h3",item.title);
            card.append(title,element("p",item.personnel_class+" · "+item.lessons.length+" ders · "+
                (item.kind==="partner"?"İş ortakları":"İşe alınan çalışanlar"),"muted"));
            result.append(card);
        }
    }
    function renderLessonDraft() {
        const box=byId("trainingLessonList");box.replaceChildren();
        draftLessons.forEach((item,index)=>{
            const line=element("p",(index+1)+". "+item.module+" / "+item.title+" · "+item.question);
            const remove=element("button","Kaldır");remove.type="button";
            remove.onclick=()=>{draftLessons.splice(index,1);renderLessonDraft()};
            line.append(" ",remove);box.append(line);
        });
        if(!draftLessons.length)box.append(element("p","Önce en az bir ders ve soru ekleyin."));
    }
    function showEligible() {
        const selector=byId("trainingPerson");
        const course=data.courses.find(x=>x.id===byId("trainingCourse").value);
        const people=course?.kind==="partner"
          ?data.completed_interviews.map(v=>[v.id,v.name+" · "+v.position])
          :data.employees.filter(v=>!course?.position_id||course.position_id===v.position_id)
              .map(v=>[v.id,v.name+" · "+v.position]);
        const replacement=picker("source_id",people,"Kişi seçin");
        replacement.id=selector.id;
        selector.replaceWith(replacement);
    }
    function showEnrollments() {
        const selector=byId("trainingCourse");
        const old=selector.value;
        selector.replaceChildren(new Option("Program seçin",""));
        data.courses.forEach(c=>selector.append(new Option(c.title+" · "+c.personnel_class,c.id)));
        if(data.courses.some(c=>c.id===old))selector.value=old;
        else if(data.courses.length)selector.value=data.courses[0].id;
        showEligible();
        const list=byId("trainingEnrollmentList");list.replaceChildren();
        if(!data.enrollments.length){list.append(element("p","Henüz eğitim daveti oluşturulmadı."));return}
        data.enrollments.forEach(item=>{
            const card=element("div","","saas-panel");
            card.append(element("strong",item.learner_name+" · "+item.title),
                element("p",item.completed+"/"+item.total+" ders tamamlandı · "+
                item.correct+" doğru"+(item.revoked?" · Erişim iptal":"")));
            const invite=element("button",item.revoked?"Erişimi yenile":"Davet bağlantısı oluştur");invite.type="button";
            invite.onclick=async()=>{
                if(!confirm("Yeni bağlantı önceki daveti ve açık eğitim oturumunu geçersiz kılar. Devam edilsin mi?"))return;
                invite.disabled=true;
                try{
                    const response=await api("enrollments/"+encodeURIComponent(item.id)+"/invite","POST",{});
                    const href=element("a","Eğitim davet bağlantısı");href.href=response.invite_url;
                    href.rel="noreferrer noopener";href.target="_blank";
                    const url=control("url");url.value=response.invite_url;url.readOnly=true;
                    const copy=element("button","Bağlantıyı kopyala");copy.type="button";
                    copy.onclick=()=>navigator.clipboard.writeText(response.invite_url).then(()=>status("Bağlantı kopyalandı."))
                        .catch(()=>status("Bağlantıyı metin alanından kopyalayın.",true));
                    card.append(url,copy,href);
                    status("Davet bağlantısı oluşturuldu; yalnızca ilgili kişiyle paylaşın.");
                }catch(e){status(e.message,true)}finally{invite.disabled=false}
            };
            const revoke=element("button","Erişimi iptal et");revoke.type="button";
            revoke.disabled=!!item.revoked;
            revoke.onclick=async()=>{
                if(!confirm("Eğitim erişimi iptal edilsin mi? Kaydedilmiş yanıtlar korunur."))return;
                try{await api("enrollments/"+encodeURIComponent(item.id)+"/revoke","POST",{});await refresh()}
                catch(e){status(e.message,true)}
            };
            card.append(invite," ",revoke);list.append(card);
        });
    }
    async function refresh(){
        try{
            const [courses,enrollments,eligible]=await Promise.all([
                api("courses"),api("enrollments"),api("eligible")]);
            data={...courses,...enrollments,...eligible};
            showCourses();showEnrollments();status("");
        }catch(e){status(e.message,true)}
    }
    function init(){
        const courseForm=byId("trainingCourseForm");
        const lessonForm=byId("trainingLessonForm");
        const kind=byId("trainingKind"),position=byId("trainingPosition");
        function updateKind(){position.disabled=kind.value==="partner"}
        kind.onchange=updateKind;updateKind();
        api("courses").then(()=>{});
        portalJson("/api/ik/positions",{cache:"no-store"}).then(result=>{
            (result.positions||[]).forEach(p=>position.append(new Option(p.name,p.id)));
        }).catch(()=>{});
        lessonForm.addEventListener("submit",event=>{
            event.preventDefault();
            const v=Object.fromEntries(new FormData(lessonForm));
            if(draftLessons.length>=100)return status("Bir programda en fazla 100 ders bulunabilir.",true);
            draftLessons.push({
                module:v.module, title:v.title,content:v.content,question:v.question,
                choices:[v.a,v.b,v.c,v.d],best:Number(v.best),explanation:v.explanation
            });
            lessonForm.reset();renderLessonDraft();status("Ders eklendi. Programı kaydedebilirsiniz.");
        });
        courseForm.addEventListener("submit",async event=>{
            event.preventDefault();
            if(!draftLessons.length)return status("Önce en az bir ders ekleyin.",true);
            const v=Object.fromEntries(new FormData(courseForm));
            const button=courseForm.querySelector("[type=submit]");button.disabled=true;
            try{
                await api("courses","POST",{title:v.title,personnel_class:v.personnel_class,
                   description:v.description,kind:v.kind,position_id:v.position_id||null,
                   lessons:draftLessons});
                draftLessons=[];renderLessonDraft();courseForm.reset();updateKind();
                await refresh();status("Eğitim programı kaydedildi.");
            }catch(e){status(e.message,true)}finally{button.disabled=false}
        });
        byId("trainingCourse").addEventListener("change",showEligible);
        byId("trainingEnrollForm").addEventListener("submit",async event=>{
            event.preventDefault();
            const source_id=byId("trainingPerson").value;
            const course_id=byId("trainingCourse").value;
            if(!source_id||!course_id)return status("Program ve kişi seçin.",true);
            const button=event.currentTarget.querySelector("[type=submit]");button.disabled=true;
            try{const result=await api("enrollments","POST",{course_id,source_id});
                await refresh();status(result.existing?"Bu kişi zaten programa kayıtlı. Davet bağlantısını yenileyebilirsiniz.":"Kayıt oluşturuldu; aşağıdan davet bağlantısını üretin.");
            }catch(e){status(e.message,true)}finally{button.disabled=false}
        });
        byId("trainingRefresh").onclick=refresh;
        renderLessonDraft();
    }
    init();
    document.addEventListener("ik-portal-screen",event=>{
        root.hidden=event.detail!=="training";
        if(event.detail==="training")refresh();
    });
}
