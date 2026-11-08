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
      const form = box.getAttribute("form") || "pick";
      const name = box.getAttribute("data-select-all") || "ids";   // the boxes' name
      document.querySelectorAll('input[form="' + form + '"][name="' + name + '"]').forEach(function (other) {
        other.checked = box.checked;
      });
    }
    if (box.matches('input[form="pick"]')) countTicked();
  });

  // Upload: "Let me choose any file" turns off the window's file filter
  // (the app still checks what it's given).
  document.addEventListener("change", function (event) {
    const box = event.target.closest("[data-any-file]");
    if (!box) return;
    const input = document.querySelector(box.getAttribute("data-any-file"));
    if (!input) return;
    if (!input.dataset.accept) input.dataset.accept = input.getAttribute("accept") || "";
    if (box.checked) input.removeAttribute("accept");
    else input.setAttribute("accept", input.dataset.accept);
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

  // Action menus (<details class="drop">): one open at a time; a click
  // elsewhere or Esc closes them; one that would run off the right edge
  // of the screen lines up with its button's right side instead.
  function closeMenus(except) {
    document.querySelectorAll("details.drop[open]").forEach(function (menu) {
      if (menu !== except) menu.open = false;
    });
  }
  document.addEventListener("toggle", function (event) {
    const menu = event.target;
    if (!menu.matches || !menu.matches("details.drop") || !menu.open) return;
    closeMenus(menu);
    menu.classList.remove("drop-right");
    const list = menu.querySelector(".drop-list");
    if (list && list.getBoundingClientRect().right > document.documentElement.clientWidth - 4) {
      menu.classList.add("drop-right");
    }
  }, true);
  document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    const inside = event.target.closest("details.drop");
    if (inside && event.target.closest(".drop-list a, .drop-list button")) {
      // Choosing an item closes its menu, once the item has done its job.
      setTimeout(function () { inside.open = false; }, 0);
      return;
    }
    closeMenus(inside);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    const open = document.querySelector("details.drop[open]");
    if (open) { open.open = false; open.querySelector("summary").focus(); }
  });

  // "Copy" buttons (data-copy="#box"): copy the box's text. The newer way
  // (navigator.clipboard) needs a secure (https) address, which the app
  // usually doesn't have, so the older way is used when it's missing.
  document.addEventListener("click", function (event) {
    const button = event.target.closest && event.target.closest("[data-copy]");
    if (!button) return;
    const box = document.querySelector(button.dataset.copy);
    if (!box) return;
    function done(ok) {
      const label = button.textContent;
      button.textContent = ok ? "Copied ✓" : "Select and copy it by hand";
      setTimeout(function () { button.textContent = label; }, 2000);
    }
    box.focus();
    box.select();
    box.setSelectionRange(0, box.value.length);   // (iPhones need this)
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(box.value).then(function () { done(true); },
                                                    function () { done(false); });
    } else {
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      done(ok);
    }
  });

  document.addEventListener("DOMContentLoaded", countTicked);
  document.addEventListener("htmx:afterSettle", countTicked);
})();
