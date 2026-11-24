export function syncSegmentIndicator(
  segmented: HTMLElement,
  immediate = false,
): boolean {
  const indicator = segmented.querySelector<HTMLElement>(
    ":scope > .segment-indicator",
  );
  const active = segmented.querySelector<HTMLElement>(
    ":scope > .segment.active",
  );
  if (!indicator || !active || active.offsetWidth === 0) return false;

  const reducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)",
  ).matches;
  indicator.style.transition = immediate || reducedMotion ? "none" : "";
  indicator.style.width = `${active.offsetWidth}px`;
  indicator.style.height = `${active.offsetHeight}px`;
  indicator.style.transform = `translate(${active.offsetLeft}px, ${active.offsetTop}px)`;
  return true;
}

