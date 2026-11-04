import { useSyncExternalStore } from "react";

const DEFAULT_MOBILE_BREAKPOINT = 768;

function subscribe(onStoreChange: () => void) {
  window.addEventListener("resize", onStoreChange);
  return () => window.removeEventListener("resize", onStoreChange);
}

function getSnapshot(breakpoint: number) {
  return window.innerWidth < breakpoint;
}

export function useIsMobile(breakpoint: number = DEFAULT_MOBILE_BREAKPOINT) {
  return useSyncExternalStore(
    subscribe,
    () => getSnapshot(breakpoint),
    () => false,
  );
}
