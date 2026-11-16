"use strict";

const picker = document.querySelector("[data-address-picker]");
if (picker) {
    const [countries, regions] = addressCatalog;
    const country = picker.querySelector("[name=country]");
    const city = picker.querySelector("[name=city]");
    const district = picker.querySelector("[name=district]");
    const manual = picker.querySelector("[name=address_manual]");
    const help = picker.querySelector(".address-help");
    const compare = new Intl.Collator("tr", { sensitivity: "base" }).compare;
    const normalize = value => value.trim().toLowerCase().normalize("NFKD")
        .replace(/\p{M}/gu, "").replace(/ı/g, "i");
    const match = (value, options) => {
        if (options.includes(value)) return value;
        const matches = options.filter(option => normalize(option) === normalize(value));
        return matches.length === 1 ? matches[0] : undefined;
    };
    function countryCode() {
        const input = normalize(country.value);
        if (["turkiye", "turkey", "tr"].includes(input)) return "TR";
        return Object.keys(countries).find(code => code.toLowerCase() === input ||
            countries[code] === match(country.value, Object.values(countries)));
    }
    function fill(input, options) {
        const entries = [...new Set(options)].sort(compare).map(value => {
            const option = document.createElement("option");
            option.value = value;
            return option;
        });
        input.list.replaceChildren(...entries);
    }
    function update() {
        const code = countryCode();
        const provinces = regions[code] || {};
        const province = match(city.value, Object.keys(provinces));
        city.labels[0].firstChild.textContent = code === "TR" ? "İl" : "Eyalet / bölge";
        district.labels[0].firstChild.textContent = code === "TR" ? "İlçe" : "Alt bölge / şehir";
        fill(city, Object.keys(provinces));
        fill(district, provinces[province] || []);
        manual.disabled = !code || code === "TR";
        help.textContent = code === "TR" ? "İl seçildiğinde yalnızca o ilin ilçeleri gösterilir."
            : "Yurt dışı bölgesi listede yoksa manuel girişi işaretleyin.";
    }
    let selectedCountry = country.value;
    let selectedCity = city.value;
    country.addEventListener("input", () => {
        if (country.value !== selectedCountry) {
            selectedCountry = country.value;
            selectedCity = city.value = district.value = "";
            manual.checked = false;
        }
        update();
    });
    city.addEventListener("input", () => {
        if (city.value !== selectedCity) {
            selectedCity = city.value;
            district.value = "";
        }
        update();
    });
    update();
}
