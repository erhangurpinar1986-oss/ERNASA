/* Close the mobile navigation on selection and on history restoration. */
function closeSalesMenu() {
  document.querySelectorAll('details.mobile-nav[open]').forEach(menu => menu.removeAttribute('open'));
}
document.addEventListener('click', event => {
  if (event.target.closest('.mobile-nav a')) closeSalesMenu();
});
window.addEventListener('pageshow', closeSalesMenu);

/* Payment links: copy the read-only link field (same behaviour as the interview invite copy). */
document.addEventListener('click', async event => {
  const button = event.target.closest('button[data-copy-target]');
  if (!button) return;
  const field = document.getElementById(button.dataset.copyTarget);
  const feedback = document.getElementById('odeme-kopya-sonuc');
  if (!field) return;
  try {
    await navigator.clipboard.writeText(field.value);
    if (feedback) feedback.textContent = 'Ödeme bağlantısı kopyalandı.';
  } catch {
    field.focus();
    field.select();
    if (feedback) feedback.textContent = 'Bağlantı seçildi. Ctrl+C ile kopyalayın.';
  }
});
