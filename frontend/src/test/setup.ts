import "@testing-library/jest-dom/vitest";

// jsdom has no ResizeObserver (a real browser API recharts' ResponsiveContainer depends on for sizing) -
// needed the first time a test renders any chart component. A no-op stub is correct here: layout/resize
// behavior isn't what these tests assert on, only that the chart renders without crashing.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// jsdom has neither Element.scrollIntoView nor pointer-capture support, both of which Radix's Select
// (used by AddComponentDialog.tsx etc.) calls internally when its listbox opens/positions itself. Needed
// the first time a test opens any shadcn/Radix Select, not specific to one component.
if (typeof Element !== "undefined") {
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {};
  }
  // jsdom has no scroll-into-view-on-new-message behavior (InfraChatPanel.tsx etc.) - a no-op is correct
  // here since these tests don't assert on scroll position.
  if (!Element.prototype.scrollTo) {
    Element.prototype.scrollTo = () => {};
  }
}
