/* Close the mobile navigation on selection and on history restoration. */
function closeSalesMenu() {
  document.querySelectorAll('details.mobile-nav[open]').forEach(menu => menu.removeAttribute('open'));
}
document.addEventListener('click', event => {
  if (event.target.closest('.mobile-nav a')) closeSalesMenu();
});
window.addEventListener('pageshow', closeSalesMenu);
