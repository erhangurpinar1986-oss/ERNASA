"use strict";

const candidateLocales = Object.freeze({tr:{dir:"ltr",enabled:true},en:{dir:"ltr",enabled:true},
    ar:{dir:"rtl",enabled:false},de:{dir:"ltr",enabled:false}});
const candidateEnglish = {
    ".brand-text p":"AI-assisted recruitment platform",
    ".secure-pill":"Secure interview",
    ".client-area span":"Employer",
    ".welcome-text .eyebrow":"ERNASA DIGITAL INTERVIEW",
    ".welcome-text > p:first-of-type":"This interview is the next step in your application. Tell us about your work and experience.",
    ".info-tile:first-child small":"Estimated duration",
    ".info-tile:first-child b":"8–10 minutes",
    ".info-tile:nth-child(2) small":"Interview",
    ".info-tile:nth-child(2) b":"10 questions",
    ".quiet-note":"Choose a quiet place. After starting, answer the questions in order.",
    "#consentCard h3":"Privacy and confirmations",
    "#consentCard .section-heading p":"Read and confirm the following before starting.",
    "[for=kvkkCheckbox] span":"I have read and understood the privacy notice (Turkish original).",
    "[for=consentCheckbox] span":"I have read and accept the consent statement (Turkish original).",
    "[for=accuracyCheckbox] span":"I confirm that the information I provide is accurate.",
    "#openKvkkButton":"Read notice",
    "#openConsentButton":"Read statement",
    "#startInterviewButton":"Start interview",
    ".button-note":"Confirm all items to continue.",
    ".question-head .eyebrow":"ERNASA DIGITAL INTERVIEW",
    "#questionTitle":"Interview question",
    ".question-heading p":"Answer using your own experience.",
    ".answer-label":"Your answer",
    "#voiceTypingHint strong":"You can dictate your answer.",
    ".answer-hint-windows":"On Windows, click the answer box and press",
    ".answer-hint-instructions":". On your phone, tap the microphone on your keyboard. You can edit the text before submitting.",
    "#submitAnswerButton":"Submit answer",
    "#modalCloseAction":"Close",
    ".footer > span:first-child":"Your data is processed under Turkish Personal Data Protection Law No. 6698.",
};
let candidateLanguage = "tr";

function candidateText(turkish, english) {
    return candidateLanguage === "en" ? english : turkish;
}

function setCandidateLocale(language) {
    if (!candidateLocales[language]?.enabled) return;
    candidateLanguage = language;
    document.documentElement.lang = language;
    document.documentElement.dir = candidateLocales[language].dir;
    if (language !== "en") return;
    document.title = "ERNASA | Digital Interview";
    for (const [selector,text] of Object.entries(candidateEnglish)) {
        document.querySelector(selector).textContent = text;
    }
    const name = document.getElementById("candidateGreeting");
    name.previousSibling.textContent = "Welcome, ";
    document.getElementById("answerText").placeholder = "Enter your answer";
}
