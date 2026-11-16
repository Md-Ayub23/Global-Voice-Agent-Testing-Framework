// Applies the saved light/dark theme before the page paints (a file, not inline, so the strict
// Content-Security-Policy can forbid inline scripts).
try { var t = localStorage.getItem("vatf-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
