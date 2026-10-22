const ernasaHighResSectors={"ik": "https://images.unsplash.com/photo-1521737711867-e3b97375f902?auto=format&fit=crop&w=1800&q=88", "saglik": "https://images.unsplash.com/photo-1576091160399-112ba8d25d1d?auto=format&fit=crop&w=1800&q=88", "lojistik": "https://images.unsplash.com/photo-1586528116311-ad8dd3c8310d?auto=format&fit=crop&w=1800&q=88", "uretim": "https://images.unsplash.com/photo-1565043589221-1a6fd9ae45c7?auto=format&fit=crop&w=1800&q=88", "finans": "https://images.unsplash.com/photo-1460925895917-afdab827c52f?auto=format&fit=crop&w=1800&q=88", "egitim": "https://images.unsplash.com/photo-1503676260728-1c00da094a0b?auto=format&fit=crop&w=1800&q=88", "gayrimenkul": "https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?auto=format&fit=crop&w=1800&q=88", "kurumsal": "https://images.unsplash.com/photo-1497366754035-f200968a6e72?auto=format&fit=crop&w=1800&q=88"};
const nav=document.querySelector('.nav');const toggle=document.querySelector('.menu-toggle');toggle?.addEventListener('click',()=>{const open=nav.classList.toggle('open');toggle.setAttribute('aria-expanded',String(open));toggle.setAttribute('aria-label',open?'Menüyü kapat':'Menüyü aç')});nav?.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>{nav.classList.remove('open');toggle?.setAttribute('aria-expanded','false')}));
document.getElementById('copy-site')?.addEventListener('click',async()=>{const el=document.getElementById('copy-status');try{await navigator.clipboard.writeText('https://ernasa.com');el.textContent='ernasa.com adresi kopyalandı.'}catch{el.textContent='Adres: https://ernasa.com'}});
const reduce=window.matchMedia('(prefers-reduced-motion: reduce)').matches;const reveals=document.querySelectorAll('.reveal');if('IntersectionObserver'in window&&!reduce){const io=new IntersectionObserver(entries=>entries.forEach(e=>{if(e.isIntersecting){e.target.classList.add('visible');io.unobserve(e.target)}}),{threshold:.08,rootMargin:'0px 0px 30px 0px'});reveals.forEach(el=>io.observe(el))}else reveals.forEach(el=>el.classList.add('visible'));
let scheduled=false;function updateProgress(){const max=document.documentElement.scrollHeight-innerHeight;document.querySelector('.reading-progress').style.width=(max>0?Math.min(100,scrollY/max*100):0)+'%';scheduled=false}window.addEventListener('scroll',()=>{if(!scheduled){requestAnimationFrame(updateProgress);scheduled=true}},{passive:true});updateProgress();

// V4.2: Pointer-responsive cinematic project cards. Works with local index.html.
if (window.matchMedia('(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)').matches) {
  document.querySelectorAll('.project-card').forEach(card => {
    let frame = 0;
    card.addEventListener('pointermove', event => {
      if (frame) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const r = card.getBoundingClientRect();
        const x = Math.max(0, Math.min(1, (event.clientX-r.left)/r.width));
        const y = Math.max(0, Math.min(1, (event.clientY-r.top)/r.height));
        card.style.setProperty('--mx', (x*100).toFixed(1)+'%');
        card.style.setProperty('--my', (y*100).toFixed(1)+'%');
        card.style.setProperty('--pan-x', ((.5-x)*20).toFixed(1)+'px');
        card.style.setProperty('--pan-y', ((.5-y)*14).toFixed(1)+'px');
        card.style.setProperty('--card-tilt-x', ((.5-y)*3).toFixed(2)+'deg');
        card.style.setProperty('--card-tilt-y', ((x-.5)*3).toFixed(2)+'deg');
      });
    }, {passive:true});
    card.addEventListener('pointerleave', () => {
      if (frame) cancelAnimationFrame(frame);
      ['--mx','--my','--pan-x','--pan-y','--card-tilt-x','--card-tilt-y'].forEach(prop=>card.style.removeProperty(prop));
    });
  });
}


// ERNASA V6 — Görselli mini tanıtım deneyimi
// V7: Detaylar tam sayfada; açılır pencere kullanılmaz.
document.addEventListener('click', function(e){
  const card=e.target.closest('[data-project],[data-expert],[data-sector]');
  if(!card) return;
  const type=card.dataset.project?'project':card.dataset.expert?'expert':'sector';
  const id=card.dataset.project||card.dataset.expert||card.dataset.sector;
  window.location.href='/corporate/detay/'+type+'-'+id+'.html';
});
document.addEventListener('keydown',function(e){
  if(e.key!=='Enter'&&e.key!==' ')return;
  const card=e.target.closest('[data-project],[data-expert],[data-sector]');
  if(card&&!['A','BUTTON'].includes(e.target.tagName)){e.preventDefault();card.click();}
});

// NEXUS 7: whole card opens its existing expert detail page.
document.querySelectorAll('.nexus-card[data-href]').forEach(card=>{card.addEventListener('click',e=>{if(!e.target.closest('a'))location.href=card.dataset.href});card.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target===card)location.href=card.dataset.href})});
