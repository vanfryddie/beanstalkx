(function () {
  "use strict";

  var overlay, box, currentSlotId = null, currentSourceRect = null;

  function init() {
    overlay = document.getElementById("slotModalOverlay");
    box = document.getElementById("slotModalBox");
    if (!overlay || !box) return;

    document.querySelectorAll(".slot-card").forEach(function (card) {
      card.addEventListener("click", function () { openModal(card); });
    });
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) closeModal();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeModal();
    });
  }

  function centeredTargetRect() {
    var w = Math.min(940, window.innerWidth * 0.94);
    var h = Math.min(640, window.innerHeight * 0.86);
    return { left: (window.innerWidth - w) / 2, top: (window.innerHeight - h) / 2, width: w, height: h };
  }

  function placeBox(rect, opacity) {
    box.style.left = rect.left + "px";
    box.style.top = rect.top + "px";
    box.style.width = rect.width + "px";
    box.style.height = rect.height + "px";
    box.style.opacity = String(opacity);
  }

  function openModal(card) {
    var slotId = card.dataset.slotId;
    currentSlotId = slotId;
    currentSourceRect = card.getBoundingClientRect();

    overlay.style.display = "flex";
    overlay.classList.remove("modal-overlay-in");
    box.innerHTML = "";
    box.style.transition = "none";
    placeBox(currentSourceRect, 0.5);
    box.offsetHeight; // force reflow so the FLIP transition below actually animates

    requestAnimationFrame(function () {
      overlay.classList.add("modal-overlay-in");
      box.style.transition = "left .3s cubic-bezier(.22,1,.36,1), top .3s cubic-bezier(.22,1,.36,1), width .3s cubic-bezier(.22,1,.36,1), height .3s cubic-bezier(.22,1,.36,1), opacity .22s ease";
      placeBox(centeredTargetRect(), 1);
    });

    fetch("/api/slots/" + slotId + "/modal")
      .then(function (r) { return r.text(); })
      .then(function (html) {
        box.innerHTML = html;
        bindModalInteractions();
      })
      .catch(function () {
        box.innerHTML = '<div class="empty-state" style="padding:40px;">Couldn\'t load this. Try again.</div>';
      });
  }

  function closeModal() {
    if (!overlay || overlay.style.display === "none") return;
    var card = document.querySelector('.slot-card[data-slot-id="' + currentSlotId + '"]');
    var rect = card ? card.getBoundingClientRect() : currentSourceRect || centeredTargetRect();
    box.style.transition = "left .24s ease, top .24s ease, width .24s ease, height .24s ease, opacity .2s ease";
    placeBox(rect, 0);
    overlay.classList.remove("modal-overlay-in");
    setTimeout(function () {
      overlay.style.display = "none";
      box.innerHTML = "";
      currentSlotId = null;
    }, 240);
  }
  window.ldocCloseModal = closeModal;

  function refreshModal() {
    if (!currentSlotId) return;
    fetch("/api/slots/" + currentSlotId + "/modal")
      .then(function (r) { return r.text(); })
      .then(function (html) {
        box.innerHTML = html;
        bindModalInteractions();
      });
  }

  // ------------------------------------------------- attendee mutations --
  window.removeAttendee = function (e, id) {
    e.stopPropagation();
    fetch("/api/slot-attendees/" + id + "/delete", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) return;
        var row = document.querySelector('.attendee-row[data-attendee-id="' + id + '"]');
        if (row) row.remove();
        var boardCard = document.querySelector('.ld-board .slot-card[data-slot-id="' + currentSlotId + '"]');
        if (boardCard) {
          var boardChip = boardCard.querySelector('.chip[data-attendee-id="' + id + '"]');
          if (boardChip) boardChip.remove();
          var count = boardCard.querySelectorAll(".chip").length;
          var countEl = boardCard.querySelector(".slot-count");
          if (countEl) countEl.textContent = count;
        }
        refreshModal();
      });
  };

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

  // The modal's own attendee panel is a list (per associate row, more
  // detail), not the tag cloud used on the compact board — that stays
  // tags everywhere else.
  function appendAttendeeRow(container, data, res) {
    var hint = container.querySelector(".drop-hint");
    if (hint) hint.remove();
    var row = document.createElement("div");
    row.className = "attendee-row chip-landing" + (res.mismatch ? " attendee-row-flagged" : "");
    row.dataset.attendeeId = res.attendee_id;
    row.innerHTML =
      '<span class="attendee-name">' + data.full_name + "</span>" +
      '<span class="attendee-login mono">' + data.employee_login + "</span>" +
      (data.fc ? '<span class="attendee-fc mono">' + data.fc + "</span>" : "") +
      (res.mismatch ? '<span class="attendee-flag" title="' + (res.mismatch_reason || "") + '">⚠️ possible mismatch</span>' : "") +
      '<button type="button" class="chip-x" onclick="removeAttendee(event, ' + res.attendee_id + ')">×</button>';
    container.appendChild(row);
    setTimeout(function () { row.classList.remove("chip-landing"); }, 420);
  }

  function afterEachAdd(card, res, data) {
    if (!res.already_enrolled) {
      var modalAttendees = document.getElementById("modalAttendees");
      if (modalAttendees) appendAttendeeRow(modalAttendees, data, res);
      var boardCard = document.querySelector('.ld-board .slot-card[data-slot-id="' + currentSlotId + '"]');
      if (boardCard) {
        appendChip(boardCard.querySelector(".slot-attendees"), data, res);
        var countEl = boardCard.querySelector(".slot-count");
        if (countEl) countEl.textContent = boardCard.querySelectorAll(".chip").length;
        boardCard.classList.add("slot-just-landed");
        setTimeout(function () { boardCard.classList.remove("slot-just-landed"); }, 500);
      }
    }
    Ldoc.markPlanned(card, res.day_index, res.start_time);
  }

  function removeChipEverywhere(attendeeId) {
    document.querySelectorAll('[data-attendee-id="' + attendeeId + '"]').forEach(function (el) { el.remove(); });
    var boardCard = document.querySelector('.ld-board .slot-card[data-slot-id="' + currentSlotId + '"]');
    if (boardCard) {
      var countEl = boardCard.querySelector(".slot-count");
      if (countEl) countEl.textContent = boardCard.querySelectorAll(".chip").length;
    }
  }

  function handleBatchResult(result) {
    updateBulkBar();
    if (result.mismatches.length) {
      Ldoc.showMismatchDialog(result.mismatches, function (undone) {
        undone.forEach(function (m) {
          removeChipEverywhere(m.attendeeId);
          Ldoc.revertPlanned(m.card);
        });
        Ldoc.toast(undone.length + " undone.", "toast-warning");
        updateBulkBar();
      });
    }
  }

  function updateBulkBar() {
    var bar = document.getElementById("modalBulkBar");
    if (!bar) return;
    var side = document.querySelector(".slot-modal-side");
    var n = side ? Ldoc.getSelected(side, ".needs-card").length : 0;
    bar.style.display = n > 0 ? "flex" : "none";
    var countEl = document.getElementById("modalBulkCount");
    if (countEl) countEl.textContent = n;
  }
  window.ldocModalBulkAdd = function () {
    var side = document.querySelector(".slot-modal-side");
    var dz = document.getElementById("modalDropzone");
    if (!side || !dz) return;
    var selected = Ldoc.getSelected(side, ".needs-card");
    if (!selected.length) return;
    Ldoc.submitAttendees(dz.dataset.slotId, selected, afterEachAdd).then(handleBatchResult);
  };

  function bindModalInteractions() {
    var side = document.querySelector(".slot-modal-side");
    if (side) {
      Ldoc.initSelectable(side, ".needs-card", updateBulkBar);
      Ldoc.bindDrag(side, '.needs-card[data-draggable="1"]', "#modalDropzone", function (slotId, group) {
        Ldoc.submitAttendees(slotId, group, afterEachAdd).then(handleBatchResult);
      });
    }

    var editForm = document.getElementById("slotEditForm");
    if (editForm && !editForm.dataset.wired) {
      editForm.dataset.wired = "1";
      editForm.addEventListener("submit", function (e) {
        e.preventDefault();
        var statusEl = document.getElementById("slotEditStatus");
        var slotId = editForm.dataset.slotId;
        var body = new URLSearchParams(new FormData(editForm));
        if (statusEl) statusEl.textContent = "Saving…";
        fetch("/ld-management/slot/" + slotId + "/edit", { method: "POST", body: body })
          .then(function (r) { return r.json(); })
          .then(function (res) {
            if (res.ok) {
              if (window.Ldoc && Ldoc.toast) Ldoc.toast("Slot updated.");
              refreshModal();
            } else if (statusEl) {
              statusEl.textContent = res.error || "Could not save.";
            }
          })
          .catch(function () { if (statusEl) statusEl.textContent = "Network error."; });
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
