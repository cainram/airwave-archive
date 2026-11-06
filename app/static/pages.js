// Small helpers for pages (the player bar has its own file, player.js).
(function () {
  "use strict";

  // Tick boxes for choosing episodes: "tick all", and a count of how many
  // are ticked.
  function countTicked() {
    const count = document.querySelectorAll('input[form="pick"][name="ids"]:checked').length;
    document.querySelectorAll("[data-picked-count]").forEach(function (place) {
      place.textContent = count ? count + " ticked" : "Tick episodes below to edit or mark them together.";
    });
  }
  document.addEventListener("change", function (event) {
    const box = event.target;
    if (box.matches("[data-select-all]")) {
      document.querySelectorAll('input[form="pick"][name="ids"]').forEach(function (other) {
        other.checked = box.checked;
      });
    }
    if (box.matches('input[form="pick"]')) countTicked();
  });

  // Bulk edit form: typing a new value picks "Change to" for that detail.
  document.addEventListener("input", function (event) {
    const field = event.target.closest("[data-bulk-field]");
    if (!field || event.target.type === "radio") return;
    const set = field.querySelector('input[type="radio"][value="set"]');
    if (set) set.checked = true;
  });

  // Uploads: show how far along a (possibly big) upload is.
  document.addEventListener("htmx:xhr:progress", function (event) {
    const form = event.target.closest && event.target.closest("[data-upload-progress]");
    const bar = form && form.querySelector("progress");
    if (!bar || !event.detail.total) return;
    bar.hidden = false;
    bar.value = Math.round(event.detail.loaded / event.detail.total * 100);
  });

  // The light / dark switch in the top bar. The choice is kept in this
  // browser (base.html applies it before the page is drawn).
  function showTheme() {
    const button = document.getElementById("theme-switch");
    if (button) button.setAttribute("aria-checked", String(document.documentElement.dataset.theme === "dark"));
  }
  document.addEventListener("click", function (event) {
    if (!event.target.closest || !event.target.closest("#theme-switch")) return;
    const dark = document.documentElement.dataset.theme !== "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    try { localStorage.setItem("airwave-theme", dark ? "dark" : "light"); } catch (e) {}
    showTheme();
  });
  document.addEventListener("DOMContentLoaded", showTheme);

  document.addEventListener("DOMContentLoaded", countTicked);
  document.addEventListener("htmx:afterSettle", countTicked);
})();
