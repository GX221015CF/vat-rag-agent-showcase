// Original implementation, MIT. Shared by the gallery, export and existing-DOM hosts.
export function attachAccordion({ button, region, content, arrow = null, duration = 340, staticMotion = false }) {
  const media = matchMedia('(prefers-reduced-motion: reduce)');
  let open = button.getAttribute('aria-expanded') === 'true';
  let destroyed = false, animations = [];
  const initialStyle = region.getAttribute('style');
  function cancel() {
    animations.forEach(animation => { animation.onfinish = null; animation.cancel(); });
    animations = [];
  }
  function move(node, frames, done) {
    if (!node.animate) { done?.(); return; }
    const animation = node.animate(frames, { duration, easing: 'ease-out' });
    animations.push(animation);
    animation.onfinish = () => {
      animations = animations.filter(item => item !== animation);
      done?.();
    };
  }
  function settle() {
    region.hidden = !open;
    region.style.height = 'auto';
  }
  function setOpen(next, { animate = true, force = false } = {}) {
    if (destroyed || (!force && open === Boolean(next))) return;
    const before = region.hidden ? 0 : region.getBoundingClientRect().height;
    cancel();
    open = Boolean(next);
    button.setAttribute('aria-expanded', String(open));
    region.setAttribute('aria-hidden', String(!open));
    region.inert = !open;
    const active = region.getRootNode().activeElement;
    if (!open && region.contains(active)) button.focus();
    region.hidden = false;
    region.style.height = 'auto';
    const target = region.getBoundingClientRect().height;
    region.style.overflow = 'hidden';
    region.style.boxSizing = 'border-box';
    if (!animate || media.matches || staticMotion || !region.animate) { settle(); return; }
    move(region, [{ height: before + 'px' }, { height: (open ? target : 0) + 'px' }], settle);
    move(content, open ? [{ opacity: 0 }, { opacity: 1 }] : [{ opacity: 1 }, { opacity: 0 }]);
    if (arrow) move(arrow, [{ transform: `rotate(${open ? 0 : 180}deg)` }, { transform: `rotate(${open ? 180 : 0}deg)` }]);
  }
  const resize = () => {
    if (destroyed || !open) return;
    // Retarget an in-flight expansion when asynchronous text changes its natural height.
    if (animations.length) setOpen(true, { force: true });
    else region.style.height = 'auto';
  };
  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
  observer?.observe(content);
  const motionChanged = () => { cancel(); settle(); };
  media.addEventListener?.('change', motionChanged);
  setOpen(open, { animate: false, force: true });
  return {
    setOpen,
    refresh: resize,
    update(options = {}) {
      duration = options.duration ?? duration;
      const nextStatic = options.staticMotion ?? staticMotion;
      if (nextStatic !== staticMotion) { cancel(); settle(); }
      staticMotion = nextStatic;
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      cancel(); observer?.disconnect(); media.removeEventListener?.('change', motionChanged);
      settle();
      if (initialStyle === null) region.removeAttribute('style'); else region.setAttribute('style', initialStyle);
      region.inert = false;
      region.removeAttribute('aria-hidden');
    },
  };
}
