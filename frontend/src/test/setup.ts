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
