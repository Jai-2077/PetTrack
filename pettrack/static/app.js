(function () {
  'use strict';
  var csrf = document.body.getAttribute('data-csrf') || '';

  // --- local time for <time class="lt">
  document.querySelectorAll('time.lt').forEach(function (t) {
    var d = new Date(t.getAttribute('datetime'));
    if (!isNaN(d)) t.textContent = d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  });

  // --- confirm dialogs on forms
  document.addEventListener('submit', function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // --- misc buttons (CSP forbids inline handlers)
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-print]')) window.print();
    if (e.target.closest('[data-reload]')) location.reload();
  });

  // --- timezone offset for SD import anchor
  var tz = document.getElementById('tz-offset');
  if (tz) tz.value = String(new Date().getTimezoneOffset());

  // --- photo preview
  var photo = document.getElementById('f-photo'), prev = document.getElementById('photo-preview');
  if (photo && prev) photo.addEventListener('change', function () {
    var f = photo.files && photo.files[0];
    if (f) { prev.src = URL.createObjectURL(f); prev.hidden = false; } else prev.hidden = true;
  });

  // --- status chips
  function setChip(name, text, cls) {
    var el = document.querySelector('[data-chip="' + name + '"]');
    if (!el) return;
    el.textContent = text;
    el.className = 'chip ' + cls;
  }
  function paintStatus(s) {
    var sc = s.scanner;
    if (sc && sc.enabled) {
      if (sc.connected) setChip('scanner', sc.rfid_ok === false ? 'Scanner: RFID fault' : 'Scanner ready', sc.rfid_ok === false ? 'warn' : 'ok');
      else setChip('scanner', 'Scanner offline', 'bad');
      var live = document.getElementById('live-text'), pulse = document.querySelector('.pulse');
      if (live) live.textContent = sc.connected ? 'Connected on ' + sc.port + (sc.fw ? ' · firmware ' + sc.fw : '') + '. Tap a tag on the reader.' : (sc.error || 'Scanner not connected. Plug in Arduino 1.');
      if (pulse) pulse.className = 'pulse ' + (sc.connected ? 'on' : 'off');
    }
    var sy = s.sync;
    if (s.mode === 'cloud') setChip('sync', 'Cloud', 'ok');
    else if (!sy.enabled) setChip('sync', 'Local only', 'off');
    else if (sy.online === false) setChip('sync', 'Offline · ' + s.unsynced + ' waiting', 'warn');
    else if (sy.online) setChip('sync', s.unsynced ? 'Syncing · ' + s.unsynced + ' waiting' : 'Synced', 'ok');
    else setChip('sync', 'Sync…', 'off');
  }
  function pollStatus() {
    fetch('/api/status', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) { if (s) paintStatus(s); })
      .catch(function () { setChip('sync', 'Server unreachable', 'bad'); });
  }
  if (document.querySelector('[data-chip]')) { pollStatus(); setInterval(pollStatus, 5000); }

  // --- live tag listener (dashboard): jump to the tapped pet
  var liveAttr = document.body.getAttribute('data-live-scan');
  if (liveAttr !== null) {
    var after = parseInt(liveAttr, 10) || 0;
    setInterval(function () {
      fetch('/api/scan-events?after=' + after, { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d || !d.events.length) return;
          var last = d.events[d.events.length - 1];
          after = d.last;
          window.location.href = '/scan/' + encodeURIComponent(last.uid);
        }).catch(function () {});
    }, 1000);
  }

  // --- service worker (needs https or localhost)
  if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  }
})();
