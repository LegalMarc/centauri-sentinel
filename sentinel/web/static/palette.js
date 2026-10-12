// Apply the saved OpenBridge palette before first paint (loaded synchronously
// in <head> on every page). Falls back to the OS light/dark preference.
(function () {
  var palettes = ["day", "dusk", "night", "bright"];
  var p = null;
  try { p = localStorage.getItem("sentinel_palette"); } catch (e) {}
  if (palettes.indexOf(p) < 0) {
    p = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dusk" : "day";
  }
  document.documentElement.setAttribute("data-obc-theme", p);
})();
