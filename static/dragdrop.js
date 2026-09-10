window.Ldoc = window.Ldoc || {};
(function (ns) {
  "use strict";

  // ---------------------------------------------------------------- toast
  function ensureToastContainer() {
    var el = document.getElementById("ldocToast");
    if (!el) {
      el = document.createElement("div");
      el.id = "ldocToast";
      document.body.appendChild(el);
    }
    return el;
  }

  ns.toast = function (msg, cls) {
    var wrap = ensureToastContainer();
    var t = document.createElement("div");
    t.className = "toast " + cls;
    t.textContent = msg;
    wrap.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("toast-in"); });
    setTimeout(function () {
      t.classList.remove("toast-in");
      setTimeout(function () { t.remove(); }, 250);
    }, 7000);
  };

  // ------------------------------------------------------------ selection
  ns.initSelectable = function (root, cardSelector, onChange) {
    root.querySelectorAll(cardSelector).forEach(function (card) {
      if (card.dataset.ldocSelBound) return;
      card.dataset.ldocSelBound = "1";
      card.addEventListener("click", function (e) {
        if (e.target.closest("button, a, input, select")) return;
        if (card.classList.contains("needs-planned") || card.dataset.noSelect) return;
        card.classList.toggle("ldoc-selected");
        if (onChange) onChange();
      });
    });
  };

  ns.getSelected = function (root, cardSelector) {
    return Array.prototype.slice.call(root.querySelectorAll(cardSelector + ".ldoc-selected"));
  };

  ns.clearSelection = function (root, cardSelector) {
    root.querySelectorAll(cardSelector + ".ldoc-selected").forEach(function (c) {
      c.classList.remove("ldoc-selected");
    });
  };

  // ---------------------------------------------------------- bulk submit
  // Adds each card's associate to a slot one at a time (so each one gets
  // its own mismatch/warning check), surfacing a toast per flagged one.
  // Adds each card's associate to a slot one at a time (so each one gets
  // its own mismatch/warning check). Anyone flagged as a possible
  // mismatch — no record of needing this topic, or already compliant /
  // not due — gets a blocking warning dialog BEFORE they're actually
  // enrolled; only proceeds if the trainer explicitly confirms "enrol
  // anyway", at which point they're added AND flagged, so a later,
  // consolidated review (ns.showMismatchDialog) can still catch and undo
  // them as a batch.
  ns.submitAttendees = function (slotId, cards, cbEach) {
    var mismatches = [];
    var chain = Promise.resolve();
    cards.forEach(function (card) {
      chain = chain.then(function () {
        var data = { employee_login: card.dataset.login, full_name: card.dataset.name, fc: card.dataset.fc };
        return ns._enrolWithConfirm(slotId, data).then(function (res) {
          if (res.ok && res.skipped) {
            return; // trainer chose not to enrol this one after seeing the warning
          }
          if (res.ok) {
            if (res.mismatch) {
              mismatches.push({
                name: data.full_name, reason: res.mismatch_reason,
                attendeeId: res.attendee_id, slotId: slotId,
                dayIndex: res.day_index, startTime: res.start_time, card: card,
              });
            }
            if (cbEach) cbEach(card, res, data);
          }
        });
      });
    });
    return chain.then(function () { return { mismatches: mismatches }; });
  };

  // Does the actual POST for one associate, showing a blocking warning
  // dialog first if the server says this one needs confirmation before
  // enrolling. Resolves to the final enrol response ({ok:true, skipped:
  // true} if the trainer declined).
  ns._enrolWithConfirm = function (slotId, data) {
    return fetch("/api/slots/" + slotId + "/attendees", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data)
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (!res.needs_confirmation) return res;
      return new Promise(function (resolve) {
        var body = document.createElement("div");
        var p = document.createElement("p");
        p.style.marginTop = "0";
        p.innerHTML = "<strong>" + (data.full_name || data.employee_login) + "</strong> might not need this training:";
        var reason = document.createElement("p");
        reason.style.cssText = "color:var(--text-lo); font-size:13px;";
        reason.textContent = res.reason || "No record shows they need this topic.";
        body.appendChild(p);
        body.appendChild(reason);
        ns.confirmDialog({
          title: "⚠️ Possible mismatch",
          body: body,
          confirmText: "Enrol anyway",
          cancelText: "Skip this one",
          danger: true,
          onConfirm: function () {
            var confirmedData = {};
            for (var k in data) confirmedData[k] = data[k];
            confirmedData.confirmed = "1";
            fetch("/api/slots/" + slotId + "/attendees", {
              method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(confirmedData)
            }).then(function (r2) { return r2.json(); }).then(resolve);
          },
          onCancel: function () { resolve({ ok: true, skipped: true }); },
        });
      });
    });
  };

  // ------------------------------------------------------------- undo --
  ns.undoAttendee = function (attendeeId) {
    return fetch("/api/slot-attendees/" + attendeeId + "/delete", { method: "POST" }).then(function (r) { return r.json(); });
  };

  // -------------------------------------------------- centered dialogs --
  ns.confirmDialog = function (opts) {
    opts = opts || {};
    var overlay = document.createElement("div");
    overlay.className = "ldoc-confirm-overlay";
    var box = document.createElement("div");
    box.className = "ldoc-confirm-box" + (opts.danger ? " ldoc-confirm-danger" : "");
    var titleEl = document.createElement("h3");
    titleEl.className = "ldoc-confirm-title";
    titleEl.textContent = opts.title || "";
    var closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "ldoc-confirm-close";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.textContent = "×";
    var bodyEl = document.createElement("div");
    bodyEl.className = "ldoc-confirm-body";
    if (typeof opts.body === "string") bodyEl.innerHTML = opts.body;
    else if (opts.body) bodyEl.appendChild(opts.body);
    var actions = document.createElement("div");
    actions.className = "ldoc-confirm-actions";

    function close() {
      overlay.classList.remove("ldoc-confirm-in");
      setTimeout(function () { overlay.remove(); }, 180);
    }
    closeBtn.addEventListener("click", close);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
    });

    if (opts.onConfirm) {
      var confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "btn " + (opts.danger ? "btn-danger" : "btn-gold");
      confirmBtn.textContent = opts.confirmText || "Yes";
      confirmBtn.addEventListener("click", function () { close(); opts.onConfirm(); });
      actions.appendChild(confirmBtn);
    }
    var cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn-ghost";
    cancelBtn.textContent = opts.cancelText || "Cancel";
    cancelBtn.addEventListener("click", function () { close(); if (opts.onCancel) opts.onCancel(); });
    actions.appendChild(cancelBtn);

    box.appendChild(closeBtn);
    box.appendChild(titleEl);
    box.appendChild(bodyEl);
    box.appendChild(actions);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    requestAnimationFrame(function () { overlay.classList.add("ldoc-confirm-in"); });
    return { close: close };
  };

  // Opens a single escalation ticket's detail in a slide panel — shared
  // between the Escalations tab and the Reporting > Escalations tab's
  // per-senior drill-down, both of which link to individual tickets.
  // Opens the "who's in this bucket" popup for an Indirect Role —
  // shared between the main Indirect Roles Overview page and any other
  // page that drills into role coverage (e.g. the AM Overview scorecard
  // tile), so there's one implementation instead of duplicating it (and
  // duplicating its bugs) per page. Wires up the returned fragment's
  // interactivity itself — a fragment's OWN embedded <script> tags never
  // Opens the "which DE Tech topics map to this Indirect Role" popup
  // from the Indirect Roles Definitions tab, and the "which Indirect
  // Roles does this ambassador train" popup from Ambassador
  // Management. Global (not defined inline in the fetched fragment)
  // because a <script> tag inside HTML injected via innerHTML never
  // executes — any interactivity for a fetched fragment has to live
  // here instead.
  // Shows the associates behind a Cross-Training path's Net Actuals
  // number (or an Internal Cross-Training target's) — shared between
  // the main Cross-Training tab and the AM Overview drill-down, since
  // both render the same xt_cards() card markup and need the same
  // click-through.
  // Generic "copy as image" for any real <table> element — reads the
  // header + currently-visible body rows directly from the live DOM
  // (so it automatically respects whatever filters/sort are currently
  // applied) and draws a clean PNG on a canvas, same as the Safety
  // ranking's dedicated renderer but reusable across any table
  // structure instead of hardcoding one page's dataset.* fields.
  // Deliberately not using an external rendering library (e.g.
  // html2canvas via CDN) — this app has zero external script
  // dependencies anywhere, on the assumption a corporate network might
  // not reliably reach a public CDN.
  ns.copyTableAsImage = function (tableEl, title, opts) {
    opts = opts || {};
    var headerCells = tableEl.querySelectorAll("thead th");
    var headers = Array.prototype.map.call(headerCells, function (th) { return th.textContent.trim(); });
    var bodyRows = Array.prototype.slice.call(tableEl.querySelectorAll("tbody tr")).filter(function (tr) {
      return tr.offsetParent !== null || (tr.style.display !== "none" && !tr.hidden);
    });
    var rows = bodyRows.map(function (tr) {
      return Array.prototype.map.call(tr.children, function (td) {
        return td.textContent.trim().replace(/\s+/g, " ");
      });
    });
    ns._renderRowsAsImage(headers, rows, title, opts);
  };

  // Draws the xt_cards() macro's structure (Cross-Training Standards
  // and Internal Cross-Training share this markup) as an actual card
  // grid matching the on-screen visual — one section per path, three
  // bordered shift cards each, rather than flattening it into a plain
  // table. Reads the "Lapsing" count and predicted line live from the
  // DOM (not a static data attribute) so it matches whatever day-window
  // the user currently has the page set to, not the page's initial value.
  ns.copyXtCardsAsImage = function (containerEl, title, opts) {
    opts = opts || {};
    var pathRows = Array.prototype.slice.call(containerEl.querySelectorAll("[data-xt-path-row]")).filter(function (el) {
      return el.offsetParent !== null || (el.style.display !== "none" && !el.hidden);
    });
    if (!pathRows.length) { opts.onEmpty && opts.onEmpty(); return; }

    var paths = pathRows.map(function (rowEl) {
      var rateEl = rowEl.querySelector(".xt-path-rate");
      var cards = Array.prototype.slice.call(rowEl.querySelectorAll("[data-xt-shift-card]")).map(function (c) {
        var countEl = c.querySelector(".xt-drop-count");
        var predictedEl = c.querySelector(".xt-drop-predicted");
        var windowEl = c.querySelector(".xt-drop-window-label");
        return {
          shiftLabel: c.dataset.shiftLabel,
          actual: c.dataset.actual, target: c.dataset.target, gap: parseInt(c.dataset.gap, 10),
          dropCount: countEl ? countEl.textContent.trim() : "0",
          dropWindow: windowEl ? windowEl.textContent.trim() : "",
          predicted: predictedEl ? predictedEl.textContent.trim().replace(/\s+/g, " ") : "",
        };
      });
      return { label: rowEl.dataset.pathLabel || "", rate: rateEl ? rateEl.textContent.trim() : "", cards: cards };
    });

    var dpr = window.devicePixelRatio || 1;
    var cardW = 230, cardGap = 12, cardH = 128, pathGap = 14;
    var leftColW = 200, padX = 16, titleH = title ? 34 : 0, rowPadY = 18;
    var cardsPerPath = paths[0].cards.length;
    var rightColW = cardsPerPath * cardW + (cardsPerPath - 1) * cardGap;
    var totalWidth = padX * 2 + leftColW + 18 + rightColW;
    var rowH = Math.max(cardH, 70) + rowPadY;
    var totalHeight = titleH + padX + paths.length * rowH + (paths.length - 1) * pathGap + padX;

    var canvas = document.createElement("canvas");
    canvas.width = totalWidth * dpr;
    canvas.height = totalHeight * dpr;
    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, totalWidth, totalHeight);
    ctx.textBaseline = "middle";

    var y = padX;
    if (title) {
      ctx.fillStyle = "#101828";
      ctx.font = "600 15px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(title, padX, titleH / 2 + 4);
      y = titleH + 8;
    }

    function roundRect(x, yy, w, h, r) {
      ctx.beginPath();
      ctx.moveTo(x + r, yy);
      ctx.arcTo(x + w, yy, x + w, yy + h, r);
      ctx.arcTo(x + w, yy + h, x, yy + h, r);
      ctx.arcTo(x, yy + h, x, yy, r);
      ctx.arcTo(x, yy, x + w, yy, r);
      ctx.closePath();
    }

    function wrapText(text, maxWidth, font) {
      ctx.font = font;
      var words = text.split(" "), lines = [], current = "";
      words.forEach(function (w) {
        var test = current ? current + " " + w : w;
        if (ctx.measureText(test).width > maxWidth && current) { lines.push(current); current = w; }
        else current = test;
      });
      if (current) lines.push(current);
      return lines;
    }

    paths.forEach(function (path, pi) {
      if (pi > 0) {
        ctx.strokeStyle = "#e5e5ea";
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(totalWidth, y); ctx.stroke();
      }
      var rowTop = y + rowPadY / 2;

      // Left column: rate, path name (wrapped), "View associates" hint
      ctx.fillStyle = "#0b5cd7";
      ctx.font = "700 11px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
      ctx.textAlign = "left";
      var ly = rowTop + 10;
      if (path.rate) { ctx.fillText(path.rate, padX, ly); ly += 20; }
      ctx.fillStyle = "#101828";
      var nameFont = "650 15px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
      wrapText(path.label, leftColW - 4, nameFont).forEach(function (line) {
        ctx.font = nameFont;
        ctx.fillText(line, padX, ly);
        ly += 19;
      });
      ctx.fillStyle = "#8e8e93";
      ctx.font = "550 10.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
      ctx.fillText("View associates →", padX, ly + 8);

      // Right column: the shift cards
      var x = padX + leftColW + 18;
      path.cards.forEach(function (c) {
        var positive = c.gap >= 0;
        var accentColor = positive ? "#248a3d" : "#d93025";

        ctx.fillStyle = positive ? "#f4faf5" : "#fdf4f3";
        roundRect(x, rowTop, cardW, cardH, 14); ctx.fill();
        ctx.strokeStyle = "#e5e5ea";
        ctx.lineWidth = 1;
        roundRect(x, rowTop, cardW, cardH, 14); ctx.stroke();
        ctx.fillStyle = accentColor;
        roundRect(x, rowTop, 3, cardH, 1.5); ctx.fill();

        var innerX = x + 15;
        ctx.fillStyle = "#3c3c43";
        ctx.font = "650 11px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.textAlign = "left";
        ctx.fillText(c.shiftLabel + " shift", innerX, rowTop + 18);
        ctx.fillStyle = "#8e8e93";
        ctx.font = "600 9px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.textAlign = "right";
        ctx.fillText("Lapsing in " + c.dropWindow + " Days", x + cardW - 15, rowTop + 18);

        ctx.textAlign = "left";
        ctx.fillStyle = "#0b5cd7";
        ctx.font = "670 24px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        var actualW = ctx.measureText(c.actual).width;
        ctx.fillText(c.actual, innerX, rowTop + 47);
        ctx.fillStyle = "#8e8e93";
        ctx.font = "400 17px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.fillText("/", innerX + actualW + 5, rowTop + 47);
        ctx.fillStyle = "#101828";
        ctx.font = "620 19px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.fillText(c.target, innerX + actualW + 15, rowTop + 47);

        ctx.font = "670 24px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.fillStyle = "#8e8e93";
        ctx.textAlign = "right";
        ctx.fillText(c.dropCount, x + cardW - 15, rowTop + 47);

        ctx.font = "600 8.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.fillStyle = "#8e8e93";
        ctx.textAlign = "left";
        ctx.fillText("ACTUAL / TARGET", innerX, rowTop + 62);

        ctx.strokeStyle = "#e5e5ea";
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(innerX, rowTop + 80); ctx.lineTo(x + cardW - 15, rowTop + 80); ctx.stroke();

        ctx.font = "680 10.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.fillStyle = accentColor;
        ctx.fillText((c.gap > 0 ? "+" : "") + c.gap + " vs target", innerX, rowTop + 96);

        ctx.font = "500 9px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.fillStyle = "#6e6e73";
        var predictedText = c.predicted.length > 34 ? c.predicted.slice(0, 33) + "…" : c.predicted;
        ctx.fillText(predictedText, innerX, rowTop + 112);

        x += cardW + cardGap;
      });
      y += rowH + pathGap;
    });

    canvas.toBlob(function (blob) {
      if (!blob) { opts.onDone && opts.onDone(false, "Failed"); return; }
      if (navigator.clipboard && window.ClipboardItem) {
        navigator.clipboard.write([new ClipboardItem({ "image/png": blob })])
          .then(function () { opts.onDone && opts.onDone(true, "Copied as image"); })
          .catch(function () { ns._downloadPng(blob, opts.filename); opts.onDone && opts.onDone(true, "Downloaded"); });
      } else {
        ns._downloadPng(blob, opts.filename);
        opts.onDone && opts.onDone(true, "Downloaded");
      }
    }, "image/png");
  };

  // Shared canvas drawing + clipboard/download logic for both readers
  // above — headers: string[], rows: string[][].
  ns._renderRowsAsImage = function (headers, rows, title, opts) {
    opts = opts || {};
    var colCount = headers.length || (rows[0] ? rows[0].length : 0);
    if (!colCount || !rows.length) {
      opts.onEmpty && opts.onEmpty();
      return;
    }

    var dpr = window.devicePixelRatio || 1;
    var measureCanvas = document.createElement("canvas");
    var mctx = measureCanvas.getContext("2d");
    mctx.font = "500 12px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
    var colWidths = [];
    for (var ci = 0; ci < colCount; ci++) {
      var maxW = mctx.measureText(headers[ci] || "").width;
      rows.forEach(function (r) { maxW = Math.max(maxW, mctx.measureText(r[ci] || "").width); });
      colWidths.push(Math.max(60, Math.min(260, maxW + 28)));
    }
    var padX = 14, rowHeight = 34, headerHeight = 34, titleHeight = title ? 34 : 0;
    var totalWidth = colWidths.reduce(function (a, b) { return a + b; }, 0);
    var totalHeight = titleHeight + headerHeight + rows.length * rowHeight + 14;

    var canvas = document.createElement("canvas");
    canvas.width = totalWidth * dpr;
    canvas.height = totalHeight * dpr;
    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, totalWidth, totalHeight);
    ctx.textBaseline = "middle";

    var y = 0;
    if (title) {
      ctx.fillStyle = "#101828";
      ctx.font = "600 14px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(title, padX, titleHeight / 2 + 4);
      y = titleHeight;
    }

    ctx.fillStyle = "#f5f6f8";
    ctx.fillRect(0, y, totalWidth, headerHeight);
    ctx.fillStyle = "#6e6e73";
    ctx.font = "600 10.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
    var x = 0;
    headers.forEach(function (h, i) {
      ctx.textAlign = "left";
      ctx.fillText(h.toUpperCase(), x + padX, y + headerHeight / 2);
      x += colWidths[i];
    });
    ctx.strokeStyle = "#101828";
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(0, y + headerHeight); ctx.lineTo(totalWidth, y + headerHeight); ctx.stroke();
    y += headerHeight;

    rows.forEach(function (r, ri) {
      if (ri % 2 === 1) { ctx.fillStyle = "#fafafa"; ctx.fillRect(0, y, totalWidth, rowHeight); }
      ctx.strokeStyle = "#e5e5ea";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, y + rowHeight); ctx.lineTo(totalWidth, y + rowHeight); ctx.stroke();
      ctx.fillStyle = "#101828";
      ctx.font = "500 12px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
      var cx = 0;
      r.forEach(function (cell, ci) {
        ctx.textAlign = "left";
        var maxChars = Math.floor((colWidths[ci] - padX * 1.5) / 6.2);
        var text = cell.length > maxChars ? cell.slice(0, Math.max(0, maxChars - 1)) + "…" : cell;
        ctx.fillText(text, cx + padX, y + rowHeight / 2);
        cx += colWidths[ci];
      });
      y += rowHeight;
    });

    canvas.toBlob(function (blob) {
      if (!blob) { opts.onDone && opts.onDone(false, "Failed"); return; }
      if (navigator.clipboard && window.ClipboardItem) {
        navigator.clipboard.write([new ClipboardItem({ "image/png": blob })])
          .then(function () { opts.onDone && opts.onDone(true, "Copied as image"); })
          .catch(function () { ns._downloadPng(blob, opts.filename); opts.onDone && opts.onDone(true, "Downloaded"); });
      } else {
        ns._downloadPng(blob, opts.filename);
        opts.onDone && opts.onDone(true, "Downloaded");
      }
    }, "image/png");
  };

  ns._downloadPng = function (blob, filename) {
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename || "table.png";
    document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(link.href);
  };

  // Wires a "Copy as image" button (data-copy-target="#tableId") — call
  // once per page/fragment; safe to call multiple times, it no-ops on
  // buttons already wired.
  ns.wireCopyAsImageButtons = function (root) {
    (root || document).querySelectorAll("[data-copy-target]").forEach(function (btn) {
      if (btn.dataset.copyWired) return;
      btn.dataset.copyWired = "1";
      btn.addEventListener("click", function () {
        var target = document.querySelector(btn.dataset.copyTarget);
        if (!target) return;
        var label = btn.querySelector("span") || btn;
        var original = label.textContent;
        var doneOpts = {
          filename: (btn.dataset.copyFilename || "table") + ".png",
          onDone: function (ok, msg) {
            label.textContent = msg;
            setTimeout(function () { label.textContent = original; }, 1600);
          },
          onEmpty: function () {
            label.textContent = "Nothing to copy";
            setTimeout(function () { label.textContent = original; }, 1600);
          },
        };
        if (btn.dataset.copyMode === "xt-cards") {
          ns.copyXtCardsAsImage(target, btn.dataset.copyTitle || "", doneOpts);
        } else if (btn.dataset.copyMode === "amb-matrix") {
          ns.copyAmbMatrixAsImage(target, btn.dataset.copyTitle || "", "attendance", doneOpts);
        } else if (btn.dataset.copyMode === "amb-hours-matrix") {
          ns.copyAmbMatrixAsImage(target, btn.dataset.copyTitle || "", "hours", doneOpts);
        } else if (btn.dataset.copyMode === "ir-table") {
          ns.copyIrTableAsImage(target, btn.dataset.copyTitle || "", doneOpts);
        } else {
          ns.copyTableAsImage(target, btn.dataset.copyTitle || "", doneOpts);
        }
      });
    });
  };

  // Reads the Ambassador Attendance matrix (department rows × shift
  // cells) into a table shape via the data attributes on each row/cell
  // — same reasoning as copyXtCardsAsImage: this is a card/matrix
  // layout, not a real <table>, and its cells nest attendee lists too
  // deep to read cleanly via plain textContent.
  // Draws the Ambassador Attendance/Practice matrix as an actual styled
  // grid matching the on-screen visual — department rows, three shift
  // columns each, with the real per-person list inside every cell
  // (attendance status, or hours severity) — rather than flattening it
  // to a single summary line per cell, which lost almost everything
  // the real page actually shows.
  ns.copyAmbMatrixAsImage = function (containerEl, title, mode, opts) {
    opts = opts || {};
    var statusColors = {
      present: "#248a3d", on_target: "#248a3d",
      at_risk: "#b65d00", absent: "#b65d00",
      critical: "#d93025", onsite_no_attend: "#d93025",
      excused: "#0b5cd7", not_recorded: "#8e8e93",
    };
    var rowEls = Array.prototype.slice.call(containerEl.querySelectorAll("[data-amb-matrix-row]")).filter(function (el) {
      return el.offsetParent !== null || (el.style.display !== "none" && !el.hidden);
    });
    if (!rowEls.length) { opts.onEmpty && opts.onEmpty(); return; }

    var departments = rowEls.map(function (rowEl) {
      var cells = Array.prototype.slice.call(rowEl.querySelectorAll("[data-amb-matrix-cell]")).map(function (c) {
        var people;
        try {
          people = JSON.parse(c.dataset[mode === "hours" ? "ambHoursPeople" : "ambPeople"] || "[]");
        } catch (e) { people = []; }
        return {
          shiftLabel: c.dataset.shiftLabel, total: c.dataset.total, stateLabel: c.dataset.stateLabel,
          credited: c.dataset.credited, required: c.dataset.required, people: people,
        };
      });
      return { department: rowEl.dataset.department || "", cells: cells };
    });

    var dpr = window.devicePixelRatio || 1;
    var colW = 260, colGap = 14, padX = 16, titleH = title ? 34 : 0, deptHeaderH = 26, headerBlockH = mode === "hours" ? 58 : 46;
    var personRowH = 30;
    var cellsPerRow = departments[0].cells.length;
    var totalWidth = padX * 2 + cellsPerRow * colW + (cellsPerRow - 1) * colGap;

    var rowHeights = departments.map(function (dept) {
      var maxPeople = Math.max.apply(null, dept.cells.map(function (c) { return c.people.length || 1; }));
      return deptHeaderH + headerBlockH + maxPeople * personRowH + 14;
    });
    var totalHeight = titleH + padX + rowHeights.reduce(function (a, b) { return a + b; }, 0) + padX;

    var canvas = document.createElement("canvas");
    canvas.width = totalWidth * dpr;
    canvas.height = totalHeight * dpr;
    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, totalWidth, totalHeight);
    ctx.textBaseline = "middle";

    function roundRect(x, yy, w, h, r) {
      ctx.beginPath();
      ctx.moveTo(x + r, yy);
      ctx.arcTo(x + w, yy, x + w, yy + h, r);
      ctx.arcTo(x + w, yy + h, x, yy + h, r);
      ctx.arcTo(x, yy + h, x, yy, r);
      ctx.arcTo(x, yy, x + w, yy, r);
      ctx.closePath();
    }

    var y = padX;
    if (title) {
      ctx.fillStyle = "#101828";
      ctx.font = "600 15px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(title, padX, titleH / 2 + 4);
      y = titleH + 8;
    }

    departments.forEach(function (dept, di) {
      if (di > 0) {
        ctx.strokeStyle = "#e5e5ea";
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(totalWidth, y); ctx.stroke();
        y += 14;
      }
      ctx.fillStyle = "#101828";
      ctx.font = "650 14px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(dept.department, padX, y + deptHeaderH / 2);
      y += deptHeaderH;

      var x = padX;
      dept.cells.forEach(function (cell) {
        var cellH = headerBlockH + Math.max(cell.people.length, 1) * personRowH + 10;
        ctx.fillStyle = "#fafafa";
        roundRect(x, y, colW, cellH, 10); ctx.fill();
        ctx.strokeStyle = "#e5e5ea";
        ctx.lineWidth = 1;
        roundRect(x, y, colW, cellH, 10); ctx.stroke();

        var innerX = x + 12;
        ctx.fillStyle = "#101828";
        ctx.font = "640 12px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.textAlign = "left";
        ctx.fillText(cell.shiftLabel + " · " + cell.total + (mode === "hours" ? "" : " Ambassadors"), innerX, y + 16);

        if (mode === "hours") {
          ctx.font = "500 10px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
          ctx.fillStyle = "#6e6e73";
          ctx.fillText(cell.stateLabel, innerX, y + 32);
          ctx.fillText(cell.credited + "/" + cell.required + "h credited", innerX, y + 46);
        } else {
          var stateColor = statusColors[
            cell.stateLabel === "Held" ? "on_target" : (cell.stateLabel === "Nobody attended" || cell.stateLabel === "No meeting" ? "critical" : "at_risk")
          ] || "#8e8e93";
          ctx.font = "600 10.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
          ctx.fillStyle = stateColor;
          ctx.fillText(cell.stateLabel, innerX, y + 32);
        }

        var py = y + headerBlockH + personRowH / 2;
        if (!cell.people.length) {
          ctx.fillStyle = "#8e8e93";
          ctx.font = "500 11px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
          ctx.fillText(mode === "hours" ? "No Process Ambassadors" : "No active Ambassadors", innerX, py);
        } else {
          cell.people.forEach(function (p) {
            ctx.fillStyle = "#101828";
            ctx.font = "560 11.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
            ctx.textAlign = "left";
            var name = p.full_name || p.login || "";
            var maxNameChars = 20;
            if (name.length > maxNameChars) name = name.slice(0, maxNameChars - 1) + "…";
            ctx.fillText(name, innerX, py - 6);
            ctx.fillStyle = "#8e8e93";
            ctx.font = "450 9.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
            ctx.fillText(p.login || "", innerX, py + 7);

            if (mode === "hours") {
              var worst = p.worst_process || {};
              var sevColor = statusColors[worst.severity] || "#8e8e93";
              ctx.textAlign = "right";
              ctx.font = "640 11.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
              ctx.fillStyle = "#101828";
              var hoursText = (worst.hours !== undefined ? Number(worst.hours).toFixed(1) : "0.0") + "h";
              ctx.fillText(hoursText, x + colW - 12, py - 6);
              ctx.fillStyle = sevColor;
              ctx.font = "600 9.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
              ctx.fillText(worst.severity_label || "", x + colW - 12, py + 7);
            } else {
              ctx.textAlign = "right";
              ctx.fillStyle = statusColors[p.attendance_status] || "#8e8e93";
              ctx.font = "600 10.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
              ctx.fillText(p.attendance_label || "", x + colW - 12, py);
            }
            py += personRowH;
          });
        }

        x += colW + colGap;
      });
      y += Math.max.apply(null, dept.cells.map(function (c) { return headerBlockH + Math.max(c.people.length, 1) * personRowH + 10; }));
    });

    canvas.toBlob(function (blob) {
      if (!blob) { opts.onDone && opts.onDone(false, "Failed"); return; }
      if (navigator.clipboard && window.ClipboardItem) {
        navigator.clipboard.write([new ClipboardItem({ "image/png": blob })])
          .then(function () { opts.onDone && opts.onDone(true, "Copied as image"); })
          .catch(function () { ns._downloadPng(blob, opts.filename); opts.onDone && opts.onDone(true, "Downloaded"); });
      } else {
        ns._downloadPng(blob, opts.filename);
        opts.onDone && opts.onDone(true, "Downloaded");
      }
    }, "image/png");
  };

  // Draws the Indirect Roles coverage table as an actual styled grid
  // matching the on-screen visual (colored ratio + percentage + meter
  // bar + 3 readiness stats per cell) rather than flattening it to
  // plain text — copyTableAsImage's generic text extraction produced
  // an unreadable jumble for this table's deeply nested cells.
  ns.copyIrTableAsImage = function (tableEl, title, opts) {
    opts = opts || {};
    var roleRows = Array.prototype.slice.call(tableEl.querySelectorAll("[data-ir-role-row]")).filter(function (el) {
      return el.offsetParent !== null || (el.style.display !== "none" && !el.hidden);
    });
    if (!roleRows.length) { opts.onEmpty && opts.onEmpty(); return; }

    var stateColors = { ok: "#248a3d", risk: "#b65d00", gap: "#d70015", muted: "#8e8e93" };
    var roles = roleRows.map(function (rowEl) {
      var cells = Array.prototype.slice.call(rowEl.querySelectorAll("[data-ir-shift-cell]")).map(function (c) {
        return {
          shiftLabel: c.dataset.shiftLabel, state: c.dataset.state,
          actual: c.dataset.actual, target: c.dataset.target, pct: c.dataset.pct,
          ready: c.dataset.ready, noPractice: c.dataset.noPractice, notTrained: c.dataset.notTrained,
        };
      });
      return { name: rowEl.dataset.roleName || "", special: rowEl.dataset.roleSpecial || "", cells: cells };
    });

    var dpr = window.devicePixelRatio || 1;
    var nameColW = 190, cellW = 190, rowH = 108, padX = 16, titleH = title ? 34 : 0, headerH = 30;
    var cellsPerRole = roles[0].cells.length;
    var totalWidth = padX * 2 + nameColW + cellsPerRole * cellW;
    var totalHeight = titleH + headerH + roles.length * rowH + padX;

    var canvas = document.createElement("canvas");
    canvas.width = totalWidth * dpr;
    canvas.height = totalHeight * dpr;
    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, totalWidth, totalHeight);
    ctx.textBaseline = "middle";

    var y = 0;
    if (title) {
      ctx.fillStyle = "#101828";
      ctx.font = "600 15px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(title, padX, titleH / 2 + 4);
      y = titleH;
    }

    ctx.fillStyle = "#f5f6f8";
    ctx.fillRect(0, y, totalWidth, headerH);
    ctx.fillStyle = "#6e6e73";
    ctx.font = "600 10px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("ROLE", padX, y + headerH / 2);
    var hx = padX + nameColW;
    roles[0].cells.forEach(function (c) {
      ctx.fillText((c.shiftLabel || "").toUpperCase() + " SHIFT", hx + 10, y + headerH / 2);
      hx += cellW;
    });
    ctx.strokeStyle = "#101828";
    ctx.lineWidth = 1.3;
    ctx.beginPath(); ctx.moveTo(0, y + headerH); ctx.lineTo(totalWidth, y + headerH); ctx.stroke();
    y += headerH;

    roles.forEach(function (role, ri) {
      if (ri % 2 === 1) { ctx.fillStyle = "#fafafa"; ctx.fillRect(0, y, totalWidth, rowH); }
      ctx.strokeStyle = "#e5e5ea";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, y + rowH); ctx.lineTo(totalWidth, y + rowH); ctx.stroke();

      ctx.fillStyle = "#101828";
      ctx.font = "640 12.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(role.name, padX, y + (role.special ? 46 : 54));
      if (role.special) {
        ctx.fillStyle = "#8e8e93";
        ctx.font = "560 9.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.fillText(role.special, padX, y + 64);
      }

      var cx = padX + nameColW;
      role.cells.forEach(function (c) {
        var color = stateColors[c.state] || stateColors.muted;
        var innerX = cx + 10;

        ctx.fillStyle = color;
        ctx.font = "620 20px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.textAlign = "left";
        var actualW = ctx.measureText(c.actual).width;
        ctx.fillText(c.actual, innerX, y + 22);
        ctx.fillStyle = "#8e8e93";
        ctx.font = "560 11px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
        ctx.fillText("/ " + c.target, innerX + actualW + 4, y + 24);

        if (c.pct !== "") {
          ctx.fillStyle = color;
          ctx.font = "690 10.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
          ctx.textAlign = "right";
          ctx.fillText(c.pct + "%", cx + cellW - 12, y + 18);
        }

        var meterW = cellW - 22, meterY = y + 34;
        ctx.fillStyle = "rgba(142,142,147,.18)";
        ctx.fillRect(innerX, meterY, meterW, 2);
        if (c.pct !== "") {
          ctx.fillStyle = color;
          ctx.fillRect(innerX, meterY, meterW * Math.min(100, parseFloat(c.pct)) / 100, 2);
        }

        var readiness = [["Ready", c.ready, "#30d158"], ["No practice", c.noPractice, "#ff9f0a"], ["Not trained", c.notTrained, "#ff5b55"]];
        var ry = y + 52;
        readiness.forEach(function (r) {
          ctx.fillStyle = r[2];
          ctx.beginPath(); ctx.arc(innerX + 3, ry, 3, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = "#6e6e73";
          ctx.font = "500 9.5px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
          ctx.textAlign = "left";
          ctx.fillText(r[0], innerX + 10, ry);
          ctx.fillStyle = "#101828";
          ctx.font = "640 10px -apple-system, Segoe UI, Roboto, Arial, sans-serif";
          ctx.textAlign = "right";
          ctx.fillText(String(r[1]), cx + cellW - 12, ry);
          ry += 16;
        });

        cx += cellW;
      });
      y += rowH;
    });

    canvas.toBlob(function (blob) {
      if (!blob) { opts.onDone && opts.onDone(false, "Failed"); return; }
      if (navigator.clipboard && window.ClipboardItem) {
        navigator.clipboard.write([new ClipboardItem({ "image/png": blob })])
          .then(function () { opts.onDone && opts.onDone(true, "Copied as image"); })
          .catch(function () { ns._downloadPng(blob, opts.filename); opts.onDone && opts.onDone(true, "Downloaded"); });
      } else {
        ns._downloadPng(blob, opts.filename);
        opts.onDone && opts.onDone(true, "Downloaded");
      }
    }, "image/png");
  };

  ns.showXtEmployeesFrom = function (baseUrl, shift, label) {
    var wrap = document.createElement("div");
    wrap.innerHTML = '<div class="empty-state" style="padding:24px 10px;">Loading…</div>';
    var panel = ns.slidePanel({ title: label, body: wrap });

    fetch(baseUrl)
      .then(function (r) { return r.text(); })
      .then(function (html) {
        wrap.innerHTML = "";
        var filterBar = document.createElement("div");
        filterBar.className = "xt-filter-bar";

        var search = document.createElement("input");
        search.type = "text";
        search.placeholder = "Search name or login…";

        var deptSelect = document.createElement("select");
        var deptDefault = document.createElement("option");
        deptDefault.value = "";
        deptDefault.textContent = "All home departments";
        deptSelect.appendChild(deptDefault);

        var shiftSelect = document.createElement("select");
        var shiftDefault = document.createElement("option");
        shiftDefault.value = "";
        shiftDefault.textContent = "All shifts";
        shiftSelect.appendChild(shiftDefault);
        ["early", "late", "night"].forEach(function (sh) {
          var opt = document.createElement("option");
          opt.value = sh;
          opt.textContent = { early: "Early", late: "Late", night: "Night" }[sh];
          shiftSelect.appendChild(opt);
        });
        if (shift) shiftSelect.value = shift;

        filterBar.appendChild(search);
        filterBar.appendChild(deptSelect);
        filterBar.appendChild(shiftSelect);

        var countLine = document.createElement("p");
        countLine.className = "xt-filter-count";

        var table = document.createElement("div");
        table.innerHTML = html;

        wrap.appendChild(filterBar);
        wrap.appendChild(countLine);
        wrap.appendChild(table);

        var copyLoginsBtn = table.querySelector("#xtEmpCopyLogins");
        if (copyLoginsBtn) {
          copyLoginsBtn.addEventListener("click", function () {
            var empRows = Array.prototype.slice.call(table.querySelectorAll(".xt-emp-row")).filter(function (tr) {
              return tr.style.display !== "none" && !tr.hidden;
            });
            var text = empRows.map(function (tr) { return tr.dataset.login; }).join("\n");
            var span = copyLoginsBtn.querySelector("span");
            var original = span.textContent;
            function fed(msg) { span.textContent = msg; setTimeout(function () { span.textContent = original; }, 1400); }
            if (navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(text).then(function () { fed("Copied"); }).catch(function () { fed("Failed"); });
            } else {
              var area = document.createElement("textarea");
              area.value = text; area.style.position = "fixed"; area.style.opacity = "0";
              document.body.appendChild(area); area.select();
              try { document.execCommand("copy"); fed("Copied"); } catch (err) { fed("Failed"); }
              area.remove();
            }
          });
        }

        var rows = Array.prototype.slice.call(table.querySelectorAll(".record-list-table tbody tr, .xt-emp-row"));
        var deptValues = {};
        rows.forEach(function (r) {
          var dept = r.dataset.dept;
          if (dept) deptValues[dept] = true;
        });
        Object.keys(deptValues).sort().forEach(function (dept) {
          var opt = document.createElement("option");
          opt.value = dept;
          opt.textContent = dept;
          deptSelect.appendChild(opt);
        });

        function applyFilters() {
          var q = search.value.trim().toLowerCase();
          var dept = deptSelect.value;
          var sh = shiftSelect.value;
          var shown = 0;
          rows.forEach(function (r) {
            var matches =
              (!q || r.dataset.name.indexOf(q) !== -1 || r.dataset.login.indexOf(q) !== -1) &&
              (!dept || r.dataset.dept === dept) &&
              (!sh || r.dataset.shift === sh);
            r.style.display = matches ? "" : "none";
            if (matches) shown++;
          });
          countLine.textContent = shown + " of " + rows.length + " shown";
        }

        search.addEventListener("input", applyFilters);
        deptSelect.addEventListener("change", applyFilters);
        shiftSelect.addEventListener("change", applyFilters);
        applyFilters();
      })
      .catch(function () {
        wrap.innerHTML = '<div class="empty-state">Couldn\'t load this. Try again.</div>';
      });
  };

  ns.showXtEmployees = function (key, shift, label, scenario) {
    ns.showXtEmployeesFrom("/api/xt-standards/" + encodeURIComponent(key) + "/trained?scenario=" + encodeURIComponent(scenario || "general"), shift, label);
  };

  ns.showInternalXtEmployees = function (targetId, shift, label) {
    ns.showXtEmployeesFrom("/api/internal-xt-targets/" + encodeURIComponent(targetId) + "/trained", shift, label);
  };

  // Wires the live "Lapsing in X days" recalculation and click-to-view
  // for every xt_cards() card within root — shared between the main
  // Cross-Training tab (root = document) and the AM Overview drill-down
  // fragment (root = the injected fragment), since both use the same
  // card markup. inputEl is the specific day-count <input> driving it;
  // pass null to skip live recalculation and just wire the click-to-view
  // (e.g. a fragment with no adjustable input of its own).
  ns.wireXtDropProjections = function (root, inputEl) {
    var projections = Array.prototype.slice.call(root.querySelectorAll(".xt-drop-projection"));

    function recalc() {
      var maxDays = inputEl ? parseInt(inputEl.value, 10) : NaN;
      if (isNaN(maxDays)) maxDays = 0;
      projections.forEach(function (el) {
        var records = JSON.parse(el.dataset.expiringRecords || "[]");
        var target = parseInt(el.dataset.target, 10) || 0;
        var dropping = records.filter(function (r) { return r.days_until <= maxDays; });
        var predicted = records.length - dropping.length;
        el.dataset.currentDropping = JSON.stringify(dropping);
        el.querySelector(".xt-drop-count").textContent = dropping.length;
        el.closest(".xt-shift-card").querySelectorAll(".xt-drop-window-label").forEach(function (s) { s.textContent = maxDays; });
        var predictedEl = el.querySelector(".xt-drop-predicted");
        predictedEl.innerHTML = "Predicted <strong>" + predicted + "</strong>/" + target + ' in <span class="xt-drop-window-label">' + maxDays + "</span> Days";
        predictedEl.style.color = predicted < target ? "var(--red)" : "var(--text-lo)";
      });
    }

    if (inputEl) {
      inputEl.addEventListener("input", recalc);
    }
    recalc();

    root.addEventListener("click", function (e) {
      var btn = e.target.closest(".xt-drop-count-btn");
      if (!btn) return;
      var el = btn.closest(".xt-drop-projection");
      var dropping = JSON.parse(el.dataset.currentDropping || "[]").sort(function (a, b) { return a.days_until - b.days_until; });
      var title = el.dataset.pathLabel + " — " + el.dataset.shiftLabel + " shift";
      var wrap = document.createElement("div");

      if (dropping.length) {
        var toolbar = document.createElement("div");
        toolbar.style.cssText = "display:flex; justify-content:flex-end; margin-bottom:8px;";
        var copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.className = "btn btn-ghost btn-sm";
        copyBtn.textContent = "Copy logins";
        copyBtn.addEventListener("click", function () {
          var text = dropping.map(function (r) { return r.login; }).join("\n");
          var original = copyBtn.textContent;
          function fed(msg) { copyBtn.textContent = msg; setTimeout(function () { copyBtn.textContent = original; }, 1400); }
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () { fed("Copied"); }).catch(function () { fed("Failed"); });
          } else {
            var area = document.createElement("textarea");
            area.value = text; area.style.position = "fixed"; area.style.opacity = "0";
            document.body.appendChild(area); area.select();
            try { document.execCommand("copy"); fed("Copied"); } catch (err) { fed("Failed"); }
            area.remove();
          }
        });
        toolbar.appendChild(copyBtn);
        wrap.appendChild(toolbar);

        var table = document.createElement("table");
        table.style.cssText = "width:100%; border-collapse:collapse; font-size:12.5px;";
        var thStyle = "text-align:left; padding:6px 8px; border-bottom:2px solid var(--navy); font-size:10px; text-transform:uppercase; color:var(--text-lo);";
        table.innerHTML =
          '<thead><tr>' +
          '<th style="' + thStyle + '">Associate</th>' +
          '<th style="' + thStyle + '">Login</th>' +
          '<th style="' + thStyle + '">Manager</th>' +
          '<th style="' + thStyle + ' text-align:right;">Hours (last 180d)</th>' +
          '<th style="' + thStyle + '">Lapses on</th>' +
          '<th style="' + thStyle + ' text-align:right;">In</th>' +
          '</tr></thead><tbody></tbody>';
        var tbody = table.querySelector("tbody");
        dropping.forEach(function (r) {
          var tr = document.createElement("tr");
          var tdStyle = "padding:6px 8px; border-bottom:1px solid var(--line);";
          var when = r.days_until <= 0
            ? '<span class="pill pill-gap">Overdue</span>'
            : r.days_until + " day" + (r.days_until !== 1 ? "s" : "");
          var hours = (r.hours_180 === null || r.hours_180 === undefined) ? "—" : Number(r.hours_180).toFixed(1);
          tr.innerHTML =
            '<td style="' + tdStyle + '">' + (r.name || "") + '</td>' +
            '<td style="' + tdStyle + '" class="mono">' + (r.login || "") + '</td>' +
            '<td style="' + tdStyle + '" class="mono">' + (r.manager_login || "—") + '</td>' +
            '<td style="' + tdStyle + ' text-align:right;" class="mono">' + hours + '</td>' +
            '<td style="' + tdStyle + '" class="mono">' + (r.expiry_date || "—") + '</td>' +
            '<td style="' + tdStyle + ' text-align:right;">' + when + '</td>';
          tbody.appendChild(tr);
        });
        wrap.appendChild(table);
      } else {
        wrap.innerHTML = '<div class="empty-state" style="padding:20px;">Nobody in this window.</div>';
      }
      ns.slidePanel({ title: title, body: wrap, wide: true });
    });
  };

  ns.manageDeTechTopicMapping = function (roleId, roleName) {
    var panel = ns.slidePanel({ title: roleName, body: '<div class="empty-state" style="padding:24px;">Loading…</div>', wide: true });
    fetch("/api/de-tech-role-mapping-form/" + roleId)
      .then(function (r) {
        if (!r.ok) { throw new Error("Could not load this (" + r.status + ")."); }
        return r.text();
      })
      .then(function (html) { panel.bodyEl.innerHTML = html; })
      .catch(function (err) { panel.bodyEl.innerHTML = '<div class="empty-state" style="padding:24px;">' + err.message + "</div>"; });
    return panel;
  };

  ns.manageAmbassadorIrRoles = function (ambassadorId, ambassadorName) {
    var panel = ns.slidePanel({ title: ambassadorName, body: '<div class="empty-state" style="padding:24px;">Loading…</div>', wide: true });
    fetch("/api/ambassador-ir-roles-form/" + ambassadorId)
      .then(function (r) {
        if (!r.ok) { throw new Error("Could not load this (" + r.status + ")."); }
        return r.text();
      })
      .then(function (html) { panel.bodyEl.innerHTML = html; })
      .catch(function (err) { panel.bodyEl.innerHTML = '<div class="empty-state" style="padding:24px;">' + err.message + "</div>"; });
    return panel;
  };

  // Both popups above submit here — via fetch, not a normal form post,
  // so saving one of many roles/ambassadors never reloads the page.
  // Delegated on document since the form only exists after a fragment
  // has been injected.
  document.addEventListener("submit", function (e) {
    var deForm = e.target.closest(".js-de-tech-mapping-form");
    var ambForm = e.target.closest(".js-amb-ir-roles-form");
    if (!deForm && !ambForm) return;
    e.preventDefault();
    var form = deForm || ambForm;
    var statusEl = form.querySelector(deForm ? ".js-de-tech-mapping-status" : ".js-amb-ir-roles-status");
    var submitBtn = form.querySelector('button[type="submit"]');
    if (statusEl) statusEl.textContent = "Saving…";
    if (submitBtn) submitBtn.disabled = true;
    fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (result) {
        if (submitBtn) submitBtn.disabled = false;
        if (!result.ok || !result.data.ok) {
          if (statusEl) statusEl.textContent = (result.data && result.data.error) || "Save failed.";
          return;
        }
        if (deForm) {
          var roleId = deForm.dataset.roleId;
          var cell = document.getElementById("deTechMappedCell-" + roleId);
          if (cell) {
            if (result.data.topics.length) {
              cell.innerHTML = '<div style="display:flex; flex-wrap:wrap; gap:4px;">' +
                result.data.topics.map(function (t) {
                  var span = document.createElement("span");
                  span.className = "pill pill-ok";
                  span.style.fontSize = "11px";
                  span.textContent = t;
                  return span.outerHTML;
                }).join("") + "</div>";
            } else {
              cell.innerHTML = '<span style="color:var(--text-lo); font-size:12px;">No topics mapped</span>';
            }
          }
        } else if (ambForm) {
          var ambassadorId = ambForm.dataset.ambassadorId;
          var countEl = document.getElementById("ambTrainsCount-" + ambassadorId);
          if (countEl) countEl.textContent = result.data.count;
          var wrapEl = document.getElementById("ambTrainsWrap-" + ambassadorId);
          if (wrapEl) {
            if (result.data.roles && result.data.roles.length) {
              wrapEl.innerHTML = result.data.roles.map(function (roleName) {
                var span = document.createElement("span");
                span.className = "pill pill-ok";
                span.style.fontSize = "10.5px";
                span.textContent = roleName;
                return span.outerHTML;
              }).join("");
            } else {
              wrapEl.innerHTML = '<span style="font-size:11px; color:var(--text-lo);">No roles assigned yet</span>';
            }
          }
        }
        if (statusEl) statusEl.textContent = "Saved ✓";
        setTimeout(function () {
          var panel = form.closest(".ldoc-slide-panel");
          if (panel) {
            var closeBtn = panel.querySelector(".ldoc-slide-close");
            if (closeBtn) closeBtn.click();
          }
        }, 500);
      })
      .catch(function () {
        if (submitBtn) submitBtn.disabled = false;
        if (statusEl) statusEl.textContent = "Save failed — check your connection and try again.";
      });
  });

  ns.openIrRoleMembers = function (roleId, shift, bucket, roleName, shiftLabel, opts) {
    opts = opts || {};
    var title = shiftLabel ? (roleName + " — " + shiftLabel) : roleName;
    var panel = ns.slidePanel({ title: title, body: '<div class="empty-state" style="padding:24px;">Loading…</div>', wide: opts.wide !== false });
    fetch("/api/ir-role-members/" + roleId + "/" + shift + "/" + bucket)
      .then(function (r) {
        if (!r.ok) { throw new Error("Could not load this (" + r.status + ")."); }
        return r.text();
      })
      .then(function (html) {
        panel.bodyEl.innerHTML = html;
        ns.wireIrMembersFragment(panel.bodyEl);
      })
      .catch(function (err) { panel.bodyEl.innerHTML = '<div class="empty-state" style="padding:24px;">' + err.message + "</div>"; });
    return panel;
  };

  // Activates the member-list fragment's checkboxes/filter/select-all
  // and (on the gap bucket) the roster-search "add more associates"
  // widget. Call this immediately after injecting the fragment's HTML
  // anywhere in the app — safe to call unconditionally; it no-ops for
  // whichever pieces aren't present in a given bucket's fragment.
  ns.wireIrMembersFragment = function (root) {
    var form = root.querySelector("#irMembersForm");
    var table = root.querySelector("#irMembersTable");
    var filter = root.querySelector("#irMemberFilter");
    var selectedCount = root.querySelector("#irSelectedCount");
    var selectVisible = root.querySelector("#irSelectVisible");
    var clearSelection = root.querySelector("#irClearSelection");

    function memberRows() { return table ? Array.prototype.slice.call(table.querySelectorAll("[data-ir-member-row]")) : []; }
    function updateSelection() {
      if (!selectedCount || !table) return;
      var count = table.querySelectorAll('input[name="member_login"]:checked').length;
      selectedCount.textContent = count + " selected";
    }

    if (form && table && filter) {
      filter.addEventListener("input", function () {
        var query = filter.value.trim().toLowerCase();
        memberRows().forEach(function (row) {
          row.hidden = !!query && (row.dataset.searchText || "").indexOf(query) === -1;
        });
      });
      form.addEventListener("change", updateSelection);
      if (selectVisible) selectVisible.addEventListener("click", function () {
        memberRows().forEach(function (row) {
          var checkbox = row.querySelector('input[name="member_login"]');
          if (!row.hidden && checkbox) checkbox.checked = true;
        });
        updateSelection();
      });
      if (clearSelection) clearSelection.addEventListener("click", function () {
        table.querySelectorAll('input[name="member_login"]').forEach(function (checkbox) { checkbox.checked = false; });
        updateSelection();
      });
      updateSelection();
    }

    var input = root.querySelector("#irRosterSearchInput");
    if (!input) return; // trained/no_practice buckets don't have the roster-search widget
    var resultsEl = root.querySelector("#irRosterSearchResults");
    var tbody = table ? table.querySelector("tbody") : null;
    var emptyRow = root.querySelector("#irMembersEmptyRow");
    var timer = null;

    function alreadyAdded(login) {
      return !!(table && table.querySelector('input[name="member_login"][value="' + CSS.escape(login) + '"]'));
    }
    function addCandidate(login, fullName) {
      if (alreadyAdded(login) || !tbody) return;
      if (emptyRow) { emptyRow.remove(); }
      var tr = document.createElement("tr");
      tr.setAttribute("data-ir-member-row", "");
      tr.dataset.searchText = (login + " " + (fullName || "")).toLowerCase();
      tr.innerHTML =
        '<td style="padding:6px 8px; border-bottom:1px solid var(--line);"><input class="ir-member-checkbox" type="checkbox" name="member_login" value="' + login + '" checked></td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid var(--line);" class="mono">' + login + "</td>" +
        '<td style="padding:6px 8px; border-bottom:1px solid var(--line);">' + (fullName || "—") + "</td>" +
        '<td style="padding:6px 8px; border-bottom:1px solid var(--line);">Added from roster search</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid var(--line); text-align:right;" class="mono">N/A</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid var(--line);">—</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid var(--line);">—</td>';
      tbody.appendChild(tr);
      if (form) form.dispatchEvent(new Event("change", { bubbles: true }));
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      var q = input.value.trim();
      if (q.length < 2) { resultsEl.innerHTML = ""; return; }
      timer = setTimeout(function () {
        fetch("/api/ir-roster-search?q=" + encodeURIComponent(q))
          .then(function (r) { return r.json(); })
          .then(function (people) {
            resultsEl.innerHTML = "";
            if (!people.length) {
              resultsEl.innerHTML = '<span style="font-size:12px; color:var(--text-lo);">No matches.</span>';
              return;
            }
            people.forEach(function (p) {
              var btn = document.createElement("button");
              btn.type = "button";
              btn.className = "btn btn-ghost btn-sm";
              btn.textContent = (p.full_name || p.login) + " (" + p.login + ")";
              btn.addEventListener("click", function () {
                addCandidate(p.login, p.full_name);
                btn.disabled = true;
                btn.textContent = "✓ " + btn.textContent;
              });
              resultsEl.appendChild(btn);
            });
          });
      }, 250);
    });
  };

  ns.openEscalationTicket = function (ticketId) {
    var panel = ns.slidePanel({ title: "Escalation ticket", body: '<div class="empty-state" style="padding:24px;">Loading…</div>' });
    fetch("/escalations/tickets/" + ticketId)
      .then(function (r) {
        if (!r.ok) { throw new Error(r.status === 403 ? "You don't have access to view this ticket." : "Could not load this ticket (" + r.status + ")."); }
        return r.text();
      })
      .then(function (html) { panel.bodyEl.innerHTML = html; })
      .catch(function (err) { panel.bodyEl.innerHTML = '<div class="empty-state" style="padding:24px;">' + err.message + "</div>"; });
  };

  // One consolidated popup listing every associate whose sign-up didn't
  // clearly match this session, with an Undo for the whole batch.
  ns.showMismatchDialog = function (mismatches, onUndoAll) {
    if (!mismatches || !mismatches.length) return;
    var list = document.createElement("div");
    list.className = "mismatch-list";
    mismatches.forEach(function (m) {
      var row = document.createElement("div");
      row.className = "mismatch-row";
      var name = document.createElement("strong");
      name.textContent = m.name;
      var reason = document.createElement("span");
      reason.textContent = m.reason || "May not need this training.";
      row.appendChild(name);
      row.appendChild(reason);
      list.appendChild(row);
    });
    return ns.confirmDialog({
      title: "⚠️ " + mismatches.length + " possible mismatch" + (mismatches.length > 1 ? "es" : ""),
      body: list,
      confirmText: "Undo all flagged",
      cancelText: "Keep them enrolled",
      danger: true,
      onConfirm: function () {
        Promise.all(mismatches.map(function (m) { return ns.undoAttendee(m.attendeeId); }))
          .then(function () { if (onUndoAll) onUndoAll(mismatches); });
      },
    });
  };

  // -------------------------------------------------- physics-based drag
  // Drags one card, or — if the dragged card is part of an active
  // selection — the whole selected group together (shown as a stacked
  // clone with a "+N" badge). dropzoneSelector is re-queried live each
  // drag so newly-rendered dropzones (e.g. after a fragment refresh) work
  // without re-binding.
  ns.bindDrag = function (root, cardSelector, dropzoneSelector, onDropped) {
    root.querySelectorAll(cardSelector).forEach(function (card) {
      if (card.dataset.ldocDragBound) return;
      card.dataset.ldocDragBound = "1";
      if (card.classList.contains("needs-planned")) return;
      card.addEventListener("pointerdown", function (e) {
        if (e.button !== undefined && e.button !== 0) return;
        if (e.target.closest("button, a, input, select")) return;
        startDrag(e, card, root, cardSelector, dropzoneSelector, onDropped);
      });
    });
  };

  function startDrag(e, card, root, cardSelector, dropzoneSelector, onDropped) {
    e.preventDefault();
    var group = card.classList.contains("ldoc-selected") ? ns.getSelected(root, cardSelector) : [card];
    if (group.indexOf(card) === -1) group.unshift(card);

    var rect = card.getBoundingClientRect();
    var clone = card.cloneNode(true);
    clone.classList.add("needs-card-clone");
    clone.removeAttribute("id");
    clone.style.position = "fixed";
    clone.style.left = rect.left + "px";
    clone.style.top = rect.top + "px";
    clone.style.width = rect.width + "px";
    clone.style.margin = "0";
    clone.style.zIndex = "99999";
    clone.style.pointerEvents = "none";
    clone.style.cursor = "grabbing";
    if (group.length > 1) {
      var badge = document.createElement("div");
      badge.className = "drag-count-badge";
      badge.textContent = "+" + (group.length - 1);
      clone.appendChild(badge);
    }
    document.body.appendChild(clone);
    group.forEach(function (c) { c.style.opacity = "0.25"; });

    var offsetX = e.clientX - rect.left;
    var offsetY = e.clientY - rect.top;
    var curX = rect.left, curY = rect.top, targetX = rect.left, targetY = rect.top, prevX = rect.left, angle = 0;
    var rafId = null, released = false, activeDz = null;

    function frame() {
      curX += (targetX - curX) * 0.32;
      curY += (targetY - curY) * 0.32;
      var dx = curX - prevX; prevX = curX;
      var desired = Math.max(-16, Math.min(16, dx * 2.4));
      angle += (desired - angle) * 0.28;
      clone.style.transform = "rotate(" + angle.toFixed(2) + "deg) scale(1.04)";
      clone.style.left = curX + "px";
      clone.style.top = curY + "px";

      activeDz = null;
      var cx = curX + clone.offsetWidth / 2, cy = curY + clone.offsetHeight / 2;
      document.querySelectorAll(dropzoneSelector).forEach(function (dz) {
        var r = dz.getBoundingClientRect();
        var near = cx > r.left - 40 && cx < r.right + 40 && cy > r.top - 40 && cy < r.bottom + 40;
        dz.classList.toggle("drop-active", near);
        if (near) activeDz = dz;
      });

      if (!released) rafId = requestAnimationFrame(frame);
    }
    rafId = requestAnimationFrame(frame);

    function onMove(ev) { targetX = ev.clientX - offsetX; targetY = ev.clientY - offsetY; }

    function onUp() {
      released = true;
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      cancelAnimationFrame(rafId);
      document.querySelectorAll(dropzoneSelector).forEach(function (dz) { dz.classList.remove("drop-active"); });

      if (activeDz) {
        var dzRect = activeDz.getBoundingClientRect();
        clone.style.transition = "left .22s cubic-bezier(.22,1,.36,1), top .22s cubic-bezier(.22,1,.36,1), transform .22s ease, opacity .22s ease";
        clone.style.left = (dzRect.left + dzRect.width / 2 - clone.offsetWidth / 2) + "px";
        clone.style.top = (dzRect.top + dzRect.height / 2 - 20) + "px";
        clone.style.transform = "rotate(0deg) scale(0.85)";
        clone.style.opacity = "0.4";
        var slotId = activeDz.dataset.slotId;
        setTimeout(function () {
          clone.remove();
          group.forEach(function (c) { c.style.opacity = "1"; });
          if (onDropped) onDropped(slotId, group);
        }, 220);
      } else {
        clone.style.transition = "left .32s cubic-bezier(.34,1.56,.64,1), top .32s cubic-bezier(.34,1.56,.64,1), transform .32s ease";
        clone.style.left = rect.left + "px";
        clone.style.top = rect.top + "px";
        clone.style.transform = "rotate(0deg) scale(1)";
        setTimeout(function () {
          clone.remove();
          group.forEach(function (c) { c.style.opacity = "1"; });
        }, 320);
      }
    }

    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
  }

  // ---------------------------------------------------- planned state --
  var DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

  ns.markPlanned = function (card, dayIndex, startTime) {
    var due = card.querySelector(".gap-due");
    if (due && card.dataset.origDue === undefined) card.dataset.origDue = due.innerHTML;
    card.removeAttribute("data-draggable");
    card.classList.remove("ldoc-selected");
    card.classList.add("needs-planned", "needs-just-planned");
    var label = "Planned";
    if (dayIndex !== null && dayIndex !== undefined && !isNaN(dayIndex)) {
      label = "Planned for " + DAY_NAMES[dayIndex] + (startTime ? " at " + startTime : "");
    }
    if (due) due.innerHTML = '<span class="pill pill-planned">' + label + "</span>";
    setTimeout(function () { card.classList.remove("needs-just-planned"); }, 900);
  };

  ns.revertPlanned = function (card) {
    card.classList.remove("needs-planned", "needs-just-planned");
    card.setAttribute("data-draggable", "1");
    var due = card.querySelector(".gap-due");
    if (due && card.dataset.origDue !== undefined) due.innerHTML = card.dataset.origDue;
  };

  // --------------------------------------- Safety Compliance drill-down --
  // Bound explicitly after the fragment is injected via innerHTML — a
  // <script> tag embedded in that HTML would never execute on its own.
  ns.bindCategoryDetail = function (root) {
    var table = root.querySelector("#catRecordTable");
    var board = root.querySelector("#catPlanBoard");

    var catXtInput = root.querySelector("#catXtDropDaysInput");
    if (catXtInput) ns.wireXtDropProjections(root, catXtInput);

    var xtTable = root.querySelector("#xtRecordTable");
    if (xtTable) {
      var xtSearch = root.querySelector("#xtFilterSearch");
      var xtFc = root.querySelector("#xtFilterFc");
      var xtDept = root.querySelector("#xtFilterDept");
      var xtProcess = root.querySelector("#xtFilterProcess");
      var xtShift = root.querySelector("#xtFilterShift");
      var xtProficiency = root.querySelector("#xtFilterProficiency");
      var xtClear = root.querySelector("#xtFilterClear");
      var xtStatus = root.querySelector("#xtFilterStatus");
      var xtEmpty = root.querySelector("#xtFilterEmpty");
      var xtRows = Array.prototype.slice.call(xtTable.querySelectorAll(".xt-record-row"));
      var xtDropWeeksInput = root.querySelector("#xtDropWeeks");
      var xtDropWeeksApply = root.querySelector("#xtDropWeeksApply");
      var xtDropClear = root.querySelector("#xtDropClear");
      var xtDropPresets = Array.prototype.slice.call(root.querySelectorAll(".xt-drop-preset"));
      var xtMaxDaysUntil = null; // null = no "drops within" filter active

      function selectedValues(select) {
        return Array.prototype.filter.call(select.options, function (o) { return o.selected; }).map(function (o) { return o.value; });
      }

      function setDropFilter(days) {
        xtMaxDaysUntil = days;
        xtDropPresets.forEach(function (btn) { btn.classList.toggle("active", parseInt(btn.dataset.days, 10) === days); });
        xtDropClear.style.display = days !== null ? "" : "none";
        applyXtFilters();
      }

      function applyXtFilters() {
        var q = (xtSearch.value || "").trim().toLowerCase();
        var fcVals = selectedValues(xtFc);
        var deptVals = selectedValues(xtDept);
        var processVals = selectedValues(xtProcess);
        var shiftVals = selectedValues(xtShift);
        var profVals = selectedValues(xtProficiency);
        var visible = 0;
        xtRows.forEach(function (row) {
          var daysUntil = row.dataset.daysUntil === "" ? null : parseInt(row.dataset.daysUntil, 10);
          var matchesDrop = xtMaxDaysUntil === null || (daysUntil !== null && daysUntil <= xtMaxDaysUntil);
          var matches = (!q || row.dataset.search.indexOf(q) !== -1)
            && (!fcVals.length || fcVals.indexOf(row.dataset.fc) !== -1)
            && (!deptVals.length || deptVals.indexOf(row.dataset.dept) !== -1)
            && (!processVals.length || processVals.indexOf(row.dataset.process) !== -1)
            && (!shiftVals.length || shiftVals.indexOf(row.dataset.shift) !== -1)
            && (!profVals.length || profVals.indexOf(row.dataset.proficiency) !== -1)
            && matchesDrop;
          row.style.display = matches ? "" : "none";
          if (matches) visible += 1;
        });
        xtStatus.textContent = "Showing " + visible + " of " + xtRows.length;
        xtEmpty.style.display = visible === 0 ? "" : "none";
        xtTable.style.display = visible === 0 ? "none" : "";
      }

      [xtSearch].forEach(function (el) { el.addEventListener("input", applyXtFilters); });
      [xtFc, xtDept, xtProcess, xtShift, xtProficiency].forEach(function (el) { el.addEventListener("change", applyXtFilters); });
      xtDropPresets.forEach(function (btn) {
        btn.addEventListener("click", function () { setDropFilter(parseInt(btn.dataset.days, 10)); });
      });
      xtDropWeeksApply.addEventListener("click", function () {
        var weeks = parseInt(xtDropWeeksInput.value, 10);
        if (!weeks || weeks < 1) return;
        setDropFilter(weeks * 7);
      });
      xtDropClear.addEventListener("click", function () {
        xtDropWeeksInput.value = "";
        setDropFilter(null);
      });
      xtClear.addEventListener("click", function () {
        xtSearch.value = "";
        [xtFc, xtDept, xtProcess, xtShift, xtProficiency].forEach(function (el) {
          Array.prototype.forEach.call(el.options, function (o) { o.selected = false; });
        });
        xtDropWeeksInput.value = "";
        xtMaxDaysUntil = null;
        xtDropPresets.forEach(function (btn) { btn.classList.remove("active"); });
        xtDropClear.style.display = "none";
        applyXtFilters();
      });
      applyXtFilters();
    }

    function appendChip(container, data, res) {
      var hint = container.querySelector(".drop-hint");
      if (hint) hint.remove();
      var chip = document.createElement("span");
      chip.className = "chip chip-landing" + (res.mismatch ? " chip-flagged" : "");
      chip.dataset.attendeeId = res.attendee_id;
      if (res.mismatch) chip.title = res.mismatch_reason || "";
      chip.innerHTML = (res.mismatch ? '<span class="chip-flag">⚠️</span>' : "") + data.full_name;
      container.appendChild(chip);
      setTimeout(function () { chip.classList.remove("chip-landing"); }, 420);
    }

    function removeChipEverywhere(attendeeId) {
      root.querySelectorAll('[data-attendee-id="' + attendeeId + '"]').forEach(function (el) {
        if (el.tagName === "TR") return; // handled separately via revertPlanned
        var slotCard = el.closest(".slot-card");
        el.remove();
        if (slotCard) {
          var countEl = slotCard.querySelector(".slot-count");
          if (countEl) countEl.textContent = slotCard.querySelectorAll(".chip").length;
        }
      });
    }

    function updateSelectionUI() {
      if (!table) return;
      var selected = ns.getSelected(table, ".record-row");
      table.querySelectorAll(".record-row").forEach(function (row) {
        var cb = row.querySelector(".record-checkbox");
        if (cb) cb.checked = row.classList.contains("ldoc-selected");
      });
      var countEl = root.querySelector("#catSelCount");
      if (countEl) {
        countEl.style.display = selected.length ? "inline" : "none";
        countEl.textContent = selected.length + " selected";
      }
      if (board) {
        board.querySelectorAll(".slot-card").forEach(function (card) {
          card.classList.toggle("slot-available", selected.length > 0);
        });
      }
    }

    if (table) {
      ns.initSelectable(table, ".record-row", updateSelectionUI);

      table.querySelectorAll(".record-unenrol").forEach(function (btn) {
        btn.addEventListener("click", function (e) {
          e.stopPropagation();
          var row = btn.closest(".record-row");
          var attendeeId = btn.dataset.attendeeId;
          ns.undoAttendee(attendeeId).then(function (res) {
            if (!res.ok) return;
            removeChipEverywhere(attendeeId);
            row.classList.remove("record-planned");
            ns.revertPlanned(row);
            ns.toast("Removed from session.", "toast-warning");
          });
        });
      });
    }

    if (board) {
      board.querySelectorAll(".slot-card").forEach(function (card) {
        card.addEventListener("click", function () {
          if (!table) return;
          var selected = ns.getSelected(table, ".record-row");
          if (!selected.length) return;
          var names = selected.map(function (r) { return r.dataset.name; });
          var body = document.createElement("div");
          var p = document.createElement("p");
          p.style.marginTop = "0";
          p.innerHTML = "Enrol <strong>" + selected.length + "</strong> associate" + (selected.length > 1 ? "s" : "") +
            " in <strong>" + card.dataset.trainingName + "</strong>?";
          var namesList = document.createElement("div");
          namesList.style.cssText = "font-size:12.5px; color:var(--text-lo); max-height:160px; overflow-y:auto;";
          namesList.textContent = names.join(", ");
          body.appendChild(p);
          body.appendChild(namesList);

          ns.confirmDialog({
            title: "Confirm enrolment",
            body: body,
            confirmText: "Yes, enrol",
            cancelText: "Cancel",
            onConfirm: function () {
              var slotId = card.dataset.slotId;
              ns.submitAttendees(slotId, selected, function (row, res, data) {
                if (!res.already_enrolled) {
                  appendChip(card.querySelector(".slot-attendees"), data, res);
                  var countEl = card.querySelector(".slot-count");
                  if (countEl) countEl.textContent = card.querySelectorAll(".chip").length;
                }
                card.classList.add("slot-just-landed");
                setTimeout(function () { card.classList.remove("slot-just-landed"); }, 500);
                row.classList.add("record-planned");
                row.dataset.attendeeId = res.attendee_id;
                ns.markPlanned(row, res.day_index, res.start_time);
              }).then(function (result) {
                updateSelectionUI();
                if (result.mismatches.length) {
                  ns.showMismatchDialog(result.mismatches, function (undone) {
                    undone.forEach(function (m) {
                      removeChipEverywhere(m.attendeeId);
                      m.card.classList.remove("record-planned");
                      ns.revertPlanned(m.card);
                    });
                    ns.toast(undone.length + " undone.", "toast-warning");
                  });
                }
              });
            },
          });
        });
      });
    }
  };

  // ------------------------------------------------- slide-in side panel --
  // For browsing/filtering a real list of records (as opposed to
  // confirmDialog, which is for a short yes/no decision) — anchored to
  // the right edge, slides in over a dim backdrop.
  ns.slidePanel = function (opts) {
    opts = opts || {};
    var overlay = document.createElement("div");
    overlay.className = "ldoc-slide-overlay";
    var panel = document.createElement("div");
    panel.className = "ldoc-slide-panel" + (opts.wide ? " ldoc-slide-panel-wide" : "");

    var header = document.createElement("div");
    header.className = "ldoc-slide-header";
    var titleEl = document.createElement("h3");
    titleEl.className = "ldoc-slide-title";
    titleEl.textContent = opts.title || "";
    var closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "ldoc-slide-close";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.textContent = "×";
    header.appendChild(titleEl);
    header.appendChild(closeBtn);

    var bodyEl = document.createElement("div");
    bodyEl.className = "ldoc-slide-body";
    if (typeof opts.body === "string") bodyEl.innerHTML = opts.body;
    else if (opts.body) bodyEl.appendChild(opts.body);

    function close() {
      panel.classList.remove("ldoc-slide-in");
      overlay.classList.remove("ldoc-slide-overlay-in");
      setTimeout(function () { overlay.remove(); }, 260);
    }
    closeBtn.addEventListener("click", close);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
    });

    panel.appendChild(header);
    panel.appendChild(bodyEl);
    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    requestAnimationFrame(function () {
      overlay.classList.add("ldoc-slide-overlay-in");
      panel.classList.add("ldoc-slide-in");
    });
    return { close: close, bodyEl: bodyEl, titleEl: titleEl };
  };

  // Full-screen meeting host view — opened by clicking a meeting card on
  // the Ambassador Meetings page. Fetches the host fragment (agenda +
  // roster + materials) and shows it filling the viewport, for a trainer
  // running the meeting live off this screen.
  ns.openMeetingHost = function (url) {
    var existing = document.getElementById("ldocMeetingHostOverlay");
    if (existing) existing.remove();

    var overlay = document.createElement("div");
    overlay.className = "ldoc-host-overlay";
    overlay.id = "ldocMeetingHostOverlay";
    overlay.innerHTML = '<div class="empty-state" style="padding:60px; color:#fff;">Loading…</div>';
    document.body.appendChild(overlay);
    document.body.style.overflow = "hidden";

    fetch(url)
      .then(function (r) { return r.text(); })
      .then(function (html) { overlay.innerHTML = html; })
      .catch(function () {
        overlay.innerHTML = '<div class="empty-state" style="padding:60px; color:#fff;">Couldn\'t load this meeting. Try again.</div>';
      });

    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { ns.closeMeetingHost(); document.removeEventListener("keydown", esc); }
    });
  };

  ns.closeMeetingHost = function () {
    var overlay = document.getElementById("ldocMeetingHostOverlay");
    if (overlay) overlay.remove();
    document.body.style.overflow = "";
  };

  // Opens a single meeting document inside the host view's material pane
  // — images and PDFs render inline; anything else (Word/PowerPoint) gets
  // a direct open/download link since browsers can't render those formats.
  ns.showMeetingDoc = function (docId, name, ext) {
    var viewer = document.getElementById("hostDocViewer");
    if (!viewer) return;
    var url = "/ambassador-meetings/documents/" + docId;
    var imageExt = ["jpg", "jpeg", "png", "gif", "webp"];
    if (ext === "pdf") {
      viewer.innerHTML = '<iframe src="' + url + '" title="' + name + '"></iframe>';
    } else if (imageExt.indexOf(ext) !== -1) {
      viewer.innerHTML = '<img src="' + url + '" alt="' + name + '">';
    } else {
      viewer.innerHTML = '<div class="empty-state" style="padding:40px;">' + name +
        ' can\'t be previewed here — <a href="' + url + '" target="_blank">open it in a new tab</a> instead.</div>';
    }
  };
})(window.Ldoc);
