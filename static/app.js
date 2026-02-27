(function () {
  function qs(sel) { return document.querySelector(sel); }
  function qsa(sel) { return Array.from(document.querySelectorAll(sel)); }

  function isEnter(e) {
    return e.key === "Enter" || e.keyCode === 13;
  }

  function focusEl(el) {
    if (!el) return;
    setTimeout(() => {
      el.focus();
      if (el.select) el.select();
    }, 20);
  }

  // Move focus with Enter across inputs inside a form
  function enableEnterToNext(form, orderSelectors) {
    const els = orderSelectors.map(s => form.querySelector(s)).filter(Boolean);
    if (!els.length) return;

    els.forEach((el, idx) => {
      el.addEventListener("keydown", (e) => {
        if (!isEnter(e)) return;
        e.preventDefault();
        const next = els[idx + 1];
        if (next) {
          focusEl(next);
        } else {
          // last field -> submit
          form.requestSubmit();
        }
      });
    });
  }

  // Remember last sublocation per warehouse in localStorage
  function setupAddRememberLocation() {
    const form = qs('form[data-page="add"]');
    if (!form) return;

    const wh = form.getAttribute("data-warehouse"); // WH1/WH2
    const locSel = qs('select[name="location"]');
    const paper = qs('input[name="paper_type"]');

    if (!wh || !locSel) return;

    const key = `last_location_${wh}`;
    const saved = localStorage.getItem(key);

    // Apply saved sublocation if exists and option is valid
    if (saved) {
      const opt = Array.from(locSel.options).find(o => o.value === saved);
      if (opt) locSel.value = saved;
    }

    // Whenever location changes, save
    locSel.addEventListener("change", () => {
      localStorage.setItem(key, locSel.value);
      // after changing location, go back to paper type (fast scan workflow)
      focusEl(paper);
    });

    // Always focus Paper Type on load
    focusEl(paper);

    // Enter-to-next workflow
    enableEnterToNext(form, [
      'input[name="paper_type"]',
      'input[name="roll_id"]',
      'input[name="weight"]',
      'select[name="location"]',
    ]);

    // Extra: if scanner is used in Roll ID, it often appends Enter
    // Our handler already moves focus.
  }

  function setupEditEnterFlow() {
    const form = qs('form[data-page="edit"]');
    if (!form) return;

    // In edit, we want Enter to flow and submit at end
    enableEnterToNext(form, [
      'input[name="paper_type"]',
      'input[name="weight"]',
      'select[name="warehouse"]',
      'select[name="location"]',
    ]);
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupAddRememberLocation();
    setupEditEnterFlow();
  });
})();
