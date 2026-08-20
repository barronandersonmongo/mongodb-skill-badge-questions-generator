/* Helpers every screen that watches a background run needs.
 *
 * These were defined once per template — five copies of showAlert, four of the
 * elapsed clock, three of setStat — which had already drifted: two copies dropped
 * the timed-label branch, and a comment explaining the clock existed in two of the
 * four. One copy, taking the elements it works on as arguments, so a screen keeps
 * its own wiring and none of its own logic.
 *
 * Attached to window rather than exported: there is no bundler here by design, and
 * a module script would not see the inline handlers the templates already use.
 */
window.UI = (function () {
  "use strict";

  // --- formatters. No DOM, no state. ---

  function formatElapsed(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const pad = (n) => String(n).padStart(2, "0");
    return pad(Math.floor(total / 3600)) + ":" + pad(Math.floor((total % 3600) / 60)) +
      ":" + pad(total % 60);
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const total = Math.max(0, Math.round(seconds));
    if (total < 60) return total + "s";
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return minutes + "m " + String(total % 60).padStart(2, "0") + "s";
    return Math.floor(minutes / 60) + "h " + String(minutes % 60).padStart(2, "0") + "m";
  }

  function formatMoney(value) {
    if (value === null || value === undefined) return "—";
    return "$" + value.toFixed(2);
  }

  // Four places, because a question costs fractions of a cent and at two it would
  // read as $0.00 — useless for comparing one run with the next.
  function formatUnitCost(value) {
    if (value === null || value === undefined) return "—";
    return "$" + value.toFixed(4);
  }

  function formatTokens(value) {
    if (value === null || value === undefined) return "—";
    if (value >= 1000000) return (value / 1000000).toFixed(1) + "M";
    if (value >= 1000) return (value / 1000).toFixed(1) + "k";
    return String(value);
  }

  /* A run's clock, kept on the server's terms.
   *
   * The browser must not time a run by its own start: a reload, or a trip to another
   * screen and back, would restart the count from zero while the run carried on. And
   * a machine whose clock is off would otherwise show a negative or wildly inflated
   * elapsed time, so the offset between the two clocks is measured and applied.
   */
  function RunClock() {
    this.startedAt = null;
    this.skewMs = 0;
    this.timer = null;
  }

  RunClock.prototype.adopt = function (state) {
    if (state.server_time) this.skewMs = Date.now() - state.server_time * 1000;
    this.startedAt = state.started_at ? state.started_at * 1000 : null;
  };

  RunClock.prototype.elapsed = function (until) {
    if (this.startedAt === null) return null;
    const end = until !== null && until !== undefined ? until : Date.now() - this.skewMs;
    return end - this.startedAt;
  };

  RunClock.prototype.tick = function (onTick) {
    onTick();
    if (this.timer === null) this.timer = setInterval(onTick, 1000);
  };

  RunClock.prototype.stop = function () {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  };

  RunClock.prototype.ticking = function () {
    return this.timer !== null;
  };

  // --- panel and alert, given the elements they act on ---

  function setStat(panel, name, value) {
    const cell = panel.querySelector('[data-stat="' + name + '"]');
    if (cell) cell.textContent = value;
  }

  /* The panel is a run's only status window, and it takes the colour the separate
   * alert above it used to carry. Two windows for one run meant two elapsed times,
   * one of them stale. */
  function setPanelState(panel, kind, message) {
    panel.classList.remove("d-none", "border-info", "border-success", "border-danger");
    if (kind) panel.classList.add("border-" + kind, "border-2");
    const line = panel.querySelector("[data-status-line]");
    if (line) line.textContent = message || "";
  }

  /* An alert, optionally carrying how long the run has been going.
   *
   * Runs take minutes. Until the first poll answers there is no server timestamp, so
   * the label is left blank rather than started at zero — a clock that begins at
   * 00:00:00 on a run that started ten minutes ago is worse than no clock.
   */
  function showAlert(alertBox, clock, kind, message, options) {
    const timed = !!(options && options.timed);
    alertBox.className = "alert alert-" + kind;
    alertBox.textContent = message;
    if (!timed) {
      if (clock) clock.stop();
      return;
    }
    const label = document.createElement("span");
    label.className = "ms-2 font-monospace";
    label.dataset.elapsed = "true";
    const current = clock.elapsed();
    label.textContent = current === null ? "" : formatElapsed(current);
    alertBox.append(label);
    clock.tick(function () {
      const shown = alertBox.querySelector("[data-elapsed]");
      const running = clock.elapsed();
      if (shown && running !== null) shown.textContent = formatElapsed(running);
    });
  }

  return {
    formatElapsed: formatElapsed,
    formatDuration: formatDuration,
    formatMoney: formatMoney,
    formatUnitCost: formatUnitCost,
    formatTokens: formatTokens,
    RunClock: RunClock,
    setStat: setStat,
    setPanelState: setPanelState,
    showAlert: showAlert,
  };
})();
