"use strict";
const portalIconPaths = {
    house:"M3 10l9-7 9 7v10H3z M9 20v-7h6v7",
    "layout-dashboard":"M3 3h8v10H3z M13 3h8v6h-8z M13 11h8v10h-8z M3 15h8v6H3z",
    "file-search":"M13 2H5v20h14v-9 M13 2v6h6l-6-6 M12 12a3 3 0 1 0 0 6 3 3 0 0 0 0-6 M14 17l3 3",
    "users-round":"M8 9a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M1 21v-3a7 7 0 0 1 14 0v3 M16 2a4 4 0 0 1 0 8 M17 14a6 6 0 0 1 6 6",
    users:"M8 9a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M1 21v-3a7 7 0 0 1 14 0v3 M16 2a4 4 0 0 1 0 8 M17 14a6 6 0 0 1 6 6",
    "messages-square":"M2 3h15v10H7l-5 4z M20 8h2v13l-4-3h-6",
    "file-up":"M13 2H5v20h14V8l-6-6v6h6 M12 18v-7 M9 14l3-3 3 3",
    "contact-round":"M4 3h16v18H4z M12 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M8 17c0-5 8-5 8 0",
    "brain-circuit":"M10 22v-4H7l-2-5H2l3-4a7 7 0 1 1 12 6v7 M12 5v6h4 M12 11v4h5 M17 8h4 M18 16h4",
    briefcase:"M3 7h18v14H3z M8 7V3h8v4 M3 13h18 M10 13v3h4v-3",
    "chart-bar":"M3 3v18h18 M6 15h3v6H6z M11 10h3v11h-3z M16 5h3v16h-3z",
    calendar:"M3 5h18v16H3z M3 10h18 M8 3v4 M16 3v4",
    clipboard:"M8 5H5v16h14V5h-3 M8 3h8v5H8z M9 12h6 M9 16h6",
    settings:"M4 6h16 M4 12h16 M4 18h16 M9 4v4 M15 10v4 M10 16v4",
    "log-out":"M10 4H4v16h6 M9 12h12 M17 8l4 4-4 4"
};
for (const placeholder of document.querySelectorAll("i[data-lucide]")) {
    const pathData = portalIconPaths[placeholder.dataset.lucide];
    if (!pathData) continue;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [name, value] of Object.entries({viewBox:"0 0 24 24",width:"20",height:"20",fill:"none",
        stroke:"currentColor","stroke-width":"1.7","stroke-linecap":"round","stroke-linejoin":"round","aria-hidden":"true"})) {
        svg.setAttribute(name,value);
    }
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d",pathData);
    svg.append(path);
    placeholder.replaceWith(svg);
}
