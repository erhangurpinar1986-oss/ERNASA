function renderJobFitAnalysisHtml(raw) {
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;",
        '"': "&quot;", "'": "&#39;"
    }[ch]));

    const text = String(raw || "").trim();
    if (!text) return "<p>Değerlendirme bulunamadı.</p>";

    const lines = text.split(/\r?\n/);
    const scores = [];
    const sections = [];
    let current = null;
    const headings = [
        "GÜÇLÜ YÖNLER",
        "GELİŞTİRİLMESİ GEREKEN ALANLAR",
        "DİKKAT EDİLMESİ GEREKEN NOKTALAR",
        "İK DEĞERLENDİRME ÖZETİ"
    ];

    for (const original of lines) {
        const line = original.trim();
        if (!line) continue;

        const score = line.match(/^[-•]?\s*(.+?):\s*(\d{1,3})(?:\s*\/\s*100)?\s*$/);
        if (score) {
            scores.push({
                name: score[1].trim(),
                value: Math.min(100, Number(score[2]))
            });
            continue;
        }

        if (/^De\u011ferlendirme Y\u00fczdeleri\s*:/i.test(line)) {
            continue;
        }

        const heading = line.replace(/:$/, "").toLocaleUpperCase("tr-TR");
        if (headings.includes(heading)) {
            current = { title: heading, lines: [] };
            sections.push(current);
            continue;
        }

        if (!current) {
            current = { title: "GENEL DEĞERLENDİRME", lines: [] };
            sections.push(current);
        }
        current.lines.push(line);
    }

    const cards = scores.length ? `
        <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:14px 0 22px;">
            ${scores.map(item => `
                <div style="border:1px solid #dbe4ee;border-radius:9px;padding:12px;background:#fff;break-inside:avoid;">
                    <div style="font-size:12px;color:#475569;margin-bottom:7px;">
                        ${escapeHtml(item.name)}
                    </div>
                    <div style="font-size:20px;font-weight:700;color:#17365d;">
                        ${item.value}<span style="font-size:12px;color:#64748b;"> / 100</span>
                    </div>
                    <div style="height:6px;background:#e2e8f0;border-radius:6px;margin-top:9px;overflow:hidden;">
                        <div style="height:100%;width:${item.value}%;background:#2563eb;"></div>
                    </div>
                </div>
            `).join("")}
        </div>` : "";

    const details = sections.filter(section => section.lines.length > 0).map(section => `
        <section style="margin:16px 0;break-inside:auto;">
            <h4 style="color:#17365d;border-bottom:1px solid #dbe4ee;padding-bottom:7px;margin:0 0 10px;break-after:avoid;">
                ${escapeHtml(section.title)}
            </h4>
            ${section.lines.map(line => `
                <p style="margin:0 0 8px;line-height:1.55;overflow-wrap:anywhere;">
                    ${escapeHtml(line)}
                </p>
            `).join("")}
        </section>
    `).join("");

    return cards + details;
}
