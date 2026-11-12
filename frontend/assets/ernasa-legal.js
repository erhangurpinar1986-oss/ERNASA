/* ERNASA ortak KVKK bilgilendirmesi — ÇEREZ RIZASI DEĞİLDİR. */
(function(){
  'use strict';
  if (document.getElementById('ernasa-legal-links')) return;
  function begin(){
    if (!document.body || document.getElementById('ernasa-legal-links')) return;
    var css = document.createElement('style');
    css.textContent = '#ernasa-legal-links{position:fixed;right:14px;bottom:12px;z-index:2147482000;'+
      'background:#122a49;color:#fff;border-radius:12px;padding:8px 12px;font:12px/1.5 system-ui,Arial;'+
      'box-shadow:0 2px 12px #0003}#ernasa-legal-links a{color:#fff;text-decoration:underline;margin:0 5px}'+
      '#ernasa-legal-notice{position:fixed;right:14px;bottom:56px;z-index:2147482001;'+
      'width:min(510px,calc(100vw - 28px));padding:16px 18px;background:#fff;color:#132e4a;'+
      'border:1px solid #b7c8da;border-radius:14px;box-shadow:0 8px 35px #0003;'+
      'font:14px/1.5 system-ui,Arial}#ernasa-legal-notice p{margin:8px 0}'+
      '#ernasa-legal-notice a{color:#145bb2;text-decoration:underline}'+
      '#ernasa-legal-close{padding:7px 13px;border:0;border-radius:7px;background:#17385f;color:white;'+
      'cursor:pointer;margin-top:7px}';
    document.head.appendChild(css);
    var links = document.createElement('nav');
    links.id = 'ernasa-legal-links';
    links.setAttribute('aria-label','Yasal metinler');
    links.innerHTML = '<a href="/kvkk">KVKK</a> · <a href="/gizlilik">Gizlilik</a>';
    document.body.appendChild(links);
    var closed = false;
    try {closed = sessionStorage.getItem('ernasa_legal_notice_closed') === '1';} catch(e) {}
    if (closed || /^\/(kvkk|gizlilik)(\.html)?\/?$/.test(location.pathname)) return;
    var pane = document.createElement('section');
    pane.id = 'ernasa-legal-notice';
    pane.setAttribute('role','region');
    pane.setAttribute('aria-label','Kişisel veriler hakkında bilgilendirme');
    pane.innerHTML = '<strong>Gizliliğinize önem veriyoruz.</strong>'+
      '<p>ERNASA sitesinde kişisel veriler, ilgili amaç ve hukuki sebeplerle işlenebilir. '+
      'Bilgi için <a href="/kvkk">KVKK Aydınlatma Metni</a> ve '+
      '<a href="/gizlilik">Gizlilik Politikası</a> sayfalarını inceleyin.</p>'+
      '<p><small>Bu bir açık rıza veya çerez izni istemi değildir.</small></p>'+
      '<button type="button" id="ernasa-legal-close">Kapat</button>';
    document.body.appendChild(pane);
    document.getElementById('ernasa-legal-close').addEventListener('click',function(){
      pane.remove();
      try {sessionStorage.setItem('ernasa_legal_notice_closed','1');} catch(e) {}
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',begin);
  else begin();
})();
