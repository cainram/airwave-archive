// Airwave Archive player bar.
//
// Any button with a `data-play` attribute plays its recording in the bar at
// the bottom of the page. The bar sits outside the part of the page that htmx
// swaps, so it keeps playing while you move between pages.
(function () {
  "use strict";

  const bar = document.getElementById("player");
  const audio = document.getElementById("player-audio");
  const title = document.getElementById("player-title");
  const subtitle = document.getElementById("player-subtitle");
  let current = null; // web address of the recording in the bar

  // Show which Play buttons belong to the recording that's playing.
  function markButtons() {
    const playing = current && !audio.paused;
    document.querySelectorAll("[data-play]").forEach(function (button) {
      button.classList.toggle("is-playing", Boolean(playing && button.dataset.src === current));
    });
  }

  function load(button) {
    current = button.dataset.src;
    audio.src = current;
    title.textContent = button.dataset.title;
    title.href = button.dataset.href;
    subtitle.textContent = button.dataset.subtitle || "";
    bar.hidden = false;
    document.body.classList.add("has-player");
    // Lets phones show the title on the lock screen.
    if ("mediaSession" in navigator && window.MediaMetadata) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: button.dataset.title,
        artist: button.dataset.subtitle || "",
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
  audio.addEventListener("ended", markButtons);
  audio.addEventListener("error", function () {
    if (!current) return;
    subtitle.textContent = "Can't play this file. This browser may not support its format.";
    markButtons();
  });
  // After htmx swaps in a new page, mark its buttons too.
  document.body.addEventListener("htmx:afterSettle", markButtons);
  document.body.addEventListener("htmx:historyRestore", markButtons);
})();
