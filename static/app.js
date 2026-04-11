(function () {
  function qs(sel, root = document) {
    return root.querySelector(sel);
  }

  function qsa(sel, root = document) {
    return Array.from(root.querySelectorAll(sel));
  }

  function isEnter(e) {
    return e.key === "Enter" || e.keyCode === 13;
  }

  function isTextLikeField(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toLowerCase();
    const type = (el.type || "").toLowerCase();

    if (tag === "textarea") return true;
    if (tag !== "input") return false;

    return [
      "text",
      "number",
      "search",
      "password",
      "email",
      "tel",
      "url"
    ].includes(type) || type === "";
  }

  function focusEl(el, shouldSelect = true) {
    if (!el) return;

    setTimeout(() => {
      try {
        el.focus({ preventScroll: false });
      } catch (_) {
        el.focus();
      }

      if (shouldSelect && isTextLikeField(el) && typeof el.select === "function") {
        el.select();
      }
    }, 20);
  }

  function getVisibleEnabledFields(selectors, root) {
    return selectors
      .map(sel => qs(sel, root))
      .filter(el => {
        if (!el) return false;
        if (el.disabled) return false;
        if (el.type === "hidden") return false;
        return true;
      });
  }

  function enableEnterToNext(form, orderSelectors, options = {}) {
    if (!form) return;

    const submitOnLast = options.submitOnLast !== false;

    function getFields() {
      return getVisibleEnabledFields(orderSelectors, form);
    }

    function moveNextFrom(currentEl) {
      const fields = getFields();
      const idx = fields.indexOf(currentEl);
      if (idx === -1) return;

      const next = fields[idx + 1];
      if (next) {
        focusEl(next);
      } else if (submitOnLast && typeof form.requestSubmit === "function") {
        form.requestSubmit();
      } else if (submitOnLast) {
        form.submit();
      }
    }

    form.addEventListener("keydown", function (e) {
      const target = e.target;
      if (!target) return;
      if (!isEnter(e)) return;

      const fields = getFields();
      if (!fields.includes(target)) return;

      if (target.tagName && target.tagName.toLowerCase() === "textarea") {
        return;
      }

      e.preventDefault();
      moveNextFrom(target);
    });
  }

  function saveToStorage(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (_) {}
  }

  function getFromStorage(key) {
    try {
      return localStorage.getItem(key);
    } catch (_) {
      return null;
    }
  }

  function optionExists(selectEl, value) {
    if (!selectEl) return false;
    return Array.from(selectEl.options).some(opt => opt.value === value);
  }

  function setSelectIfValid(selectEl, value) {
    if (!selectEl || !value) return false;
    if (!optionExists(selectEl, value)) return false;
    selectEl.value = value;
    return true;
  }

  function persistLocationByWarehouse(warehouse, location) {
    if (!warehouse || !location) return;
    saveToStorage(`last_location_${warehouse}`, location);
  }

  function getSavedLocationByWarehouse(warehouse) {
    if (!warehouse) return null;
    return getFromStorage(`last_location_${warehouse}`);
  }

  function setupGlobalAutoFocus() {
    const auto = qs("[autofocus]");
    if (auto) {
      focusEl(auto);
      return;
    }

    const loginUser = qs('input[name="username"]');
    if (loginUser) {
      focusEl(loginUser);
      return;
    }

    const searchInput = qs('input[name="q"]');
    if (searchInput) {
      focusEl(searchInput);
      return;
    }

    const removeInput = qs('input[name="roll_id"]');
    if (removeInput && removeInput.closest("form")) {
      focusEl(removeInput);
    }
  }

  function setupAddRememberLocation() {
    const form = qs('form[data-page="add"]');
    if (!form) return;

    const wh = form.getAttribute("data-warehouse");
    const paper = qs('input[name="paper_type"]', form);
    const roll = qs('input[name="roll_id"]', form);
    const weight = qs('input[name="weight"]', form);
    const locSel = qs('select[name="location"]', form);

    if (!wh || !locSel) return;

    const savedLoc = getSavedLocationByWarehouse(wh);
    if (savedLoc) {
      setSelectIfValid(locSel, savedLoc);
    }

    locSel.addEventListener("change", function () {
      persistLocationByWarehouse(wh, locSel.value);
      if (paper) focusEl(paper);
    });

    enableEnterToNext(form, [
      'input[name="paper_type"]',
      'input[name="roll_id"]',
      'input[name="weight"]',
      'select[name="location"]'
    ]);

    form.addEventListener("submit", function () {
      persistLocationByWarehouse(wh, locSel.value);
    });

    if (paper) {
      focusEl(paper);
    } else if (roll) {
      focusEl(roll);
    } else if (weight) {
      focusEl(weight);
    } else {
      focusEl(locSel, false);
    }
  }

  function setupEditEnterFlow() {
    const form = qs('form[data-page="edit"]');
    if (!form) return;

    const warehouseEl = qs('select[name="warehouse"]', form);
    const locationEl = qs('select[name="location"]', form);

    enableEnterToNext(form, [
      'input[name="paper_type"]',
      'input[name="weight"]',
      'select[name="warehouse"]',
      'select[name="location"]'
    ]);

    if (warehouseEl && locationEl) {
      warehouseEl.addEventListener("change", function () {
        const wh = warehouseEl.value;
        const savedLoc = getSavedLocationByWarehouse(wh);

        setTimeout(() => {
          if (savedLoc) {
            setSelectIfValid(locationEl, savedLoc);
          }
        }, 30);
      });

      form.addEventListener("submit", function () {
        persistLocationByWarehouse(warehouseEl.value, locationEl.value);
      });
    }
  }

  function setupTransferFlow() {
    const form = qs("#transferForm");
    if (!form) return;

    const fromWh = qs('select[name="from_wh"]', form);
    const toWh = qs('select[name="to_wh"]', form);
    const location = qs('select[name="location"]', form);
    const rollId = qs('input[name="roll_id"]', form);

    if (!fromWh || !toWh || !location || !rollId) return;

    function restoreSavedTransferLocation() {
      const targetWh = toWh.value;
      const savedLoc = getSavedLocationByWarehouse(targetWh);
      if (savedLoc) {
        setTimeout(() => {
          setSelectIfValid(location, savedLoc);
        }, 30);
      }
    }

    toWh.addEventListener("change", function () {
      restoreSavedTransferLocation();
    });

    location.addEventListener("change", function () {
      persistLocationByWarehouse(toWh.value, location.value);
      focusEl(rollId);
    });

    enableEnterToNext(form, [
      'select[name="from_wh"]',
      'select[name="to_wh"]',
      'select[name="location"]',
      'input[name="roll_id"]'
    ]);

    form.addEventListener("submit", function () {
      persistLocationByWarehouse(toWh.value, location.value);
    });

    setTimeout(() => {
      restoreSavedTransferLocation();
      focusEl(rollId);
    }, 40);
  }

  function setupRestoreFlow() {
    const form = qs('form[action*="restore"], form');
    const warehouseEl = qs('#warehouse');
    const sublocationEl = qs('#sublocation');

    if (!warehouseEl || !sublocationEl) return;

    function restoreSaved() {
      const wh = warehouseEl.value;
      const savedLoc = getSavedLocationByWarehouse(wh);
      if (savedLoc) {
        setTimeout(() => {
          setSelectIfValid(sublocationEl, savedLoc);
        }, 30);
      }
    }

    warehouseEl.addEventListener("change", function () {
      restoreSaved();
    });

    sublocationEl.addEventListener("change", function () {
      persistLocationByWarehouse(warehouseEl.value, sublocationEl.value);
    });

    if (form) {
      form.addEventListener("submit", function () {
        persistLocationByWarehouse(warehouseEl.value, sublocationEl.value);
      });
    }

    setTimeout(restoreSaved, 40);
  }

  function setupRemoveSingleFocus() {
    const form = qsa("form").find(f => qs('input[name="roll_id"]', f) && !f.matches('[data-page="add"]'));
    if (!form) return;

    const rollInput = qs('input[name="roll_id"]', form);
    if (!rollInput) return;

    focusEl(rollInput);
  }

  function setupBatchTextareaFocus() {
    const ta = qs('textarea[name="roll_ids"]');
    if (!ta) return;
    focusEl(ta, false);
  }

  function setupSearchFlow() {
    const form = qsa("form").find(f => qs('input[name="q"]', f));
    if (!form) return;

    const q = qs('input[name="q"]', form);
    if (!q) return;

    focusEl(q);
  }

  function setupClickableRowHoverEnhancement() {
    qsa("table tbody tr").forEach(row => {
      row.addEventListener("mouseenter", function () {
        row.style.transition = "background .12s ease";
      });
    });
  }

  function setupInventoryQuickEnhancement() {
    const table = qs("#inventory-table");
    if (!table) return;

    const rows = qsa("tbody tr[data-roll-id]", table);
    rows.forEach(row => {
      const rollId = row.getAttribute("data-roll-id");
      if (!rollId) return;
      row.setAttribute("title", `Roll ID: ${rollId}`);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupGlobalAutoFocus();
    setupAddRememberLocation();
    setupEditEnterFlow();
    setupTransferFlow();
    setupRestoreFlow();
    setupRemoveSingleFocus();
    setupBatchTextareaFocus();
    setupSearchFlow();
    setupClickableRowHoverEnhancement();
    setupInventoryQuickEnhancement();
  });
})();
