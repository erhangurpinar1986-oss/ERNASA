"use strict";

const inviteLink = document.getElementById("ernasa-invite-url");
const feedback = document.getElementById("ernasa-invite-copy-feedback");

async function copyField(field) {
    if (!field) return;
    try {
        await navigator.clipboard.writeText(field.value);
        feedback.textContent = "Bilgi kopyalandı.";
    } catch {
        field.focus();
        field.select();
        feedback.textContent = "Alan seçildi. Ctrl+C ile kopyalayın.";
    }
}

document.getElementById("ernasa-invite-copy").addEventListener("click", () => copyField(inviteLink));
for (const name of ["to", "subject", "body"]) {
    const button = document.getElementById("ernasa-invite-copy-" + name);
    const field = document.getElementById("ernasa-invite-" + name);
    if (button && field) button.addEventListener("click", () => copyField(field));
}
