// Airwave Archive player bar.
//
// Any button with a `data-play` attribute plays its recording in the bar at
// the bottom of the page. The bar sits outside the part of the page that htmx
// swaps, so it keeps playing while you move between pages.
//
// An episode made of several files has a `data-queue` (the files' web
// addresses, in order): when one part ends, the next one starts.
//
// Continue listening: while an episode plays, its place is sent to the app
// every few seconds and when paused (`/listening/<episode>`). Its Play
// button says where to pick up (`data-resume-file`, `data-resume-at`); a
// place remembered here since the page was made wins if it's newer.
(function () {
  "use strict";

  const bar = document.getElementById("player");
  const audio = document.getElementById("player-audio");
  const title = document.getElementById("player-title");
  const subtitle = document.getElementById("player-subtitle");
  const art = document.getElementById("player-art");
  const toggle = document.getElementById("player-toggle");
  const seek = document.getElementById("player-seek");
  const timeNow = document.getElementById("player-time");
  const timeAll = document.getElementById("player-length");
  const BACK = 15, FORWARD = 30;   // seconds the skip buttons jump
  // A picture that can't be found (no artwork) just isn't shown.
  art.addEventListener("error", function () { art.hidden = true; });
  art.addEventListener("load", function () { art.hidden = false; });
  let current = null; // the Play button's recording (first in its queue)
  let queue = [];      // web addresses still to play, in order
  let numbers = [];    // their part numbers
  let index = 0;       // which one is playing
  let parts = 0;       // how many parts the episode has (0 = just one file)
  let info = {};       // title, link, subtitle of what's playing
  let episode = "";    // the episode's number in the app ("" for inbox files)
  let startAt = 0;     // seconds to jump to once the part is ready
  let lastSaved = 0;
  const places = {};   // episode -> {file, at, when}: places saved since the page loaded
  const SAVE_EVERY = 10000, REWIND = 3;   // milliseconds; seconds

  // Show which Play buttons belong to the recording that's playing.
  function markButtons() {
    const playing = current && !audio.paused;
    document.querySelectorAll("[data-play]").forEach(function (button) {
      button.classList.toggle("is-playing", Boolean(playing && button.dataset.src === current));
    });
    toggle.classList.toggle("is-playing", !audio.paused);
    toggle.setAttribute("aria-label", audio.paused ? "Play" : "Pause");
  }

  function words(text) {
    return (text || "").trim().split(/\s+/).filter(Boolean);
  }

  function fileOf(address) {
    const found = /\/audio\/(\d+)/.exec(address || "");
    return found ? found[1] : "";
  }

  // Where to start: the button's resume point, or a newer one remembered here.
  function startingPlace(button) {
    let place = { file: button.dataset.resumeFile || "", at: Number(button.dataset.resumeAt || 0) };
    const mine = places[button.dataset.episode];
    if (mine && mine.when > Number(button.dataset.listened || 0)) {
      place = { file: mine.file, at: Math.max(0, mine.at - REWIND) };
    }
    const i = queue.map(fileOf).indexOf(place.file);
    return i < 0 ? { index: 0, at: 0 } : { index: i, at: place.at };
  }

  // Tell the app where listening is (or that it reached the end).
  function save(finished) {
    if (!episode || !queue.length) return;
    // Not before it has jumped to where it should start (or would forget the place).
    if (!finished && (startAt > 0 || !(audio.currentTime > 0))) return;
    const file = fileOf(queue[index]);
    const at = finished ? 0 : audio.currentTime || 0;
    places[episode] = { file: finished ? "" : file, at: at, when: Date.now() };
    lastSaved = Date.now();
    const body = new FormData();
    body.append("file", file);
    body.append("at", at.toFixed(1));
    if (finished) body.append("finished", "1");
    fetch("/listening/" + episode, { method: "POST", body: body, keepalive: true })
      .catch(function () { /* offline: the next save tries again */ });
  }

  function load(button) {
    if (episode && audio.currentTime > 0 && !audio.ended) save(false);   // the one being left
    current = button.dataset.src;
    episode = button.dataset.episode || "";
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
    const place = startingPlace(button);
    show(place.index);
    startAt = place.at;
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

  // ---- The player bar's own controls ----

  function clock(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
    const mm = h ? String(m).padStart(2, "0") : String(m);
    return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
  }

  let dragging = false;   // while the finger is on the progress bar
  function showTime() {
    const length = isFinite(audio.duration) ? audio.duration : 0;
    seek.max = String(Math.floor(length));
    if (!dragging) seek.value = String(Math.floor(audio.currentTime));
    timeNow.textContent = clock(dragging ? Number(seek.value) : audio.currentTime);
    timeAll.textContent = clock(length);
    seek.style.setProperty("--done", length ? (Number(seek.value) / length * 100) + "%" : "0%");
  }
  audio.addEventListener("timeupdate", showTime);
  audio.addEventListener("loadedmetadata", function () {
    if (startAt > 0) {   // picking up where listening stopped
      const length = isFinite(audio.duration) ? audio.duration : Infinity;
      audio.currentTime = Math.min(startAt, Math.max(0, length - 1));
      startAt = 0;
    }
    showTime();
  });
  audio.addEventListener("timeupdate", function () {
    if (!audio.paused && Date.now() - lastSaved > SAVE_EVERY) save(false);
  });
  audio.addEventListener("emptied", showTime);
  seek.addEventListener("input", function () { dragging = true; showTime(); });
  seek.addEventListener("change", function () {
    audio.currentTime = Number(seek.value);
    dragging = false;
    showTime();
  });

  function skip(seconds) {
    const length = isFinite(audio.duration) ? audio.duration : Infinity;
    audio.currentTime = Math.min(Math.max(0, audio.currentTime + seconds), length);
    showTime();
  }
  toggle.addEventListener("click", function () {
    if (audio.paused) audio.play().catch(function () { /* the error handler explains */ });
    else audio.pause();
  });
  document.getElementById("player-back").addEventListener("click", function () { skip(-BACK); });
  document.getElementById("player-forward").addEventListener("click", function () { skip(FORWARD); });

  // Close: stop, keep the place (so Resume still works), and hide the bar
  // until something is played again.
  document.getElementById("player-close").addEventListener("click", function () {
    if (episode && audio.currentTime > 0 && !audio.ended) save(false);
    current = null;
    episode = "";
    queue = [];
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    bar.hidden = true;
    document.body.classList.remove("has-player");
    if ("mediaSession" in navigator) navigator.mediaSession.metadata = null;
    markButtons();
  });

  // The lock screen's buttons do the same.
  if ("mediaSession" in navigator) {
    const actions = {
      play: function () { audio.play(); },
      pause: function () { audio.pause(); },
      seekbackward: function (details) { skip(-(details.seekOffset || BACK)); },
      seekforward: function (details) { skip(details.seekOffset || FORWARD); },
      seekto: function (details) { audio.currentTime = details.seekTime; showTime(); },
    };
    Object.keys(actions).forEach(function (action) {
      try { navigator.mediaSession.setActionHandler(action, actions[action]); } catch (e) { /* not supported */ }
    });
  }

  audio.addEventListener("play", markButtons);
  audio.addEventListener("pause", function () {
    if (!audio.ended) save(false);   // (at the very end, "ended" saves instead)
    markButtons();
  });
  audio.addEventListener("ended", function () {
    if (index + 1 < queue.length) {
      show(index + 1);    // on to the next part
      audio.play().catch(function () { /* the error handler below explains */ });
    } else {
      save(true);         // heard to the end: played
      markButtons();
    }
  });
  // Leaving the page or locking the phone: save the place now.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden" && audio.currentTime > 0 && !audio.ended) save(false);
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
