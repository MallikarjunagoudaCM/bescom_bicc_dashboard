document.addEventListener('DOMContentLoaded', () => {
  const shortcuts = [
    { key: 'm', wrapId: 'overview-level-mode-wrap', cls: 'is-open' }
  ];

  document.addEventListener('keydown', (e) => {
	  
    const key = (e.key || '').toLowerCase();
    const ctrlLike = e.ctrlKey || e.metaKey;
    if (!ctrlLike) return;

    const targetTag = (e.target && e.target.tagName || '').toLowerCase();
    const typing = ['input', 'textarea', 'select'].includes(targetTag) || (e.target && e.target.isContentEditable);
    if (typing) return;

    const hit = shortcuts.find(s => key === s.key);
    if (!hit) return;

    e.preventDefault();
    const el = document.getElementById(hit.wrapId);
    if (!el) return;
    el.classList.toggle(hit.cls);
    el.setAttribute('aria-hidden', el.classList.contains(hit.cls) ? 'false' : 'true');
  });
});