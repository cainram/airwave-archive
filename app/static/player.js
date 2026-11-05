// Airwave Archive player bar.
//
// Any button with a `data-play` attribute plays its recording in the bar at
// the bottom of the page. The bar sits outside the part of the page that htmx
// swaps, so it keeps playing while you move between pages.
//
// An episode made of several files has a `data-queue` (the files' web
// addresses, in order): when one part ends, the next one starts.
(function () {
  "use strict";

  const bar = document.getElementById("player");
  const audio = document.getElementById("player-audio");
  const title = document.getElementById("player-title");
  const subtitle = document.getElementById("player-subtitle");
  const art = document.getElementById("player-art");
  // A picture that can't be found (no artwork) just isn't shown.
  art.addEventListener("error", function () { art.hidden = true; });
  art.addEventListener("load", function () { art.hidden = false; });
  let current = null; // the Play button's recording (first in its queue)
  let queue = [];      // web addresses still to play, in order
  let numbers = [];    // their part numbers
  let index = 0;       // which one is playing
  let parts = 0;       // how many parts the episode has (0 = just one file)
  let info = {};       // title, link, subtitle of what's playing

  // Show which Play buttons belong to the recording that's playing.
  function markButtons() {
    const playing = current && !audio.paused;
    document.querySelectorAll("[data-play]").forEach(function (button) {
      button.classList.toggle("is-playing", Boolean(playing && button.dataset.src === current));
    });
  }

  function words(text) {
    return (text || "").trim().split(/\s+/).filter(Boolean);
  }

  function load(button) {
    current = button.dataset.src;
    queue = words(button.dataset.queue);
    if (!queue.length) queue = [current];
    numbers = words(button.dataset.numbers);
    parts = Number(button.dataset.parts || 0);
    info = {
      title: button.dataset.title,
      href: button.dataset.href,
      subtitle: button.dataset.subtitle || "",
      art: button.dataset.art || "",
    };
    art.hidden = true;
    if (info.art) art.src = info.art; else art.removeAttribute("src");
    show(0);
    bar.hidden = false;
    document.body.classList.add("has-player");
  }

  // Put part number `i` of the queue in the player.
  function show(i) {
    index = i;
    audio.src = queue[i];
    let sub = info.subtitle;
    if (parts > 1) {
      const part = "Part " + (numbers[i] || i + 1) + " of " + parts;
      sub = sub ? part + " · " + sub : part;
    }
    title.textContent = info.title;
    title.href = info.href;
    subtitle.textContent = sub;
    // Lets phones show the title on the lock screen.
    if ("mediaSession" in navigator && window.MediaMetadata) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: info.title,
        artist: sub,
        // The show's picture on the lock screen (if it has one).
        artwork: info.art ? [{ src: new URL(info.art, location.href).href, type: "image/jpeg" }] : [],
      });
    }
  }

  document.addEventListener("click", function (event) {
    const button = event.target.closest("[data-play]");
    if (!button) return;
    event.preventDefault();
    if (button.dataset.src === current && !audio.paused) {
      audio.pause();           // clicking the playing one pauses it
      return;
    }
    if (button.dataset.src !== current) load(button);
    audio.play().catch(function () { /* the error handler below explains */ });
  });

  audio.addEventListener("play", markButtons);
  audio.addEventListener("pause", markButtons);
  audio.addEventListener("ended", function () {
    if (index + 1 < queue.length) {
      show(index + 1);    // on to the next part
      audio.play().catch(function () { /* the error handler below explains */ });
    } else {
      markButtons();
    }
  });
  audio.addEventListener("error", function () {
    if (!current) return;
    subtitle.textContent = "Can't play this file. This browser may not support its format.";
    markButtons();
  });
  // After htmx swaps in a new page, mark its buttons too.
  document.body.addEventListener("htmx:afterSettle", markButtons);
  document.body.addEventListener("htmx:historyRestore", markButtons);

  // Going Back restores a page from the browser's memory, without a fresh
  // menu, so highlight the right section of the menu here.
  document.body.addEventListener("htmx:historyRestore", function () {
    const path = window.location.pathname;
    document.querySelectorAll("#menu a").forEach(function (link) {
      const here = link.dataset.paths.split(" ").some(function (start) {
        return path === start || (start !== "/" && path.startsWith(start));
      });
      if (here) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  });
})();
