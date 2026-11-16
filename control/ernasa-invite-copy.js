"use strict";

const inviteLink = document.getElementById("ernasa-invite-url");
const feedback = document.getElementById("ernasa-invite-copy-feedback");
document.getElementById("ernasa-invite-copy").addEventListener("click", async () => {
    try {
        await navigator.clipboard.writeText(inviteLink.value);
        feedback.textContent = "Bağlantı kopyalandı.";
    } catch {
        inviteLink.select();
        feedback.textContent = "Bağlantı seçildi. Ctrl+C ile kopyalayın.";
    }
});
