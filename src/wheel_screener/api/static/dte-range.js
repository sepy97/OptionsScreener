/* Two-thumb DTE window.
 *
 * The number boxes are what the form submits; the range inputs are unnamed and only drive
 * them. So with this file absent or blocked the control degrades to two working number
 * inputs rather than to nothing — the same reason the markup keeps them.
 */
(function () {
  "use strict";

  function setup(field) {
    var max = +field.dataset.max || 120;
    var fill = field.querySelector("[data-fill]");
    var ticks = field.querySelectorAll(".dte-tick");
    var thumb = { min: field.querySelector('[data-thumb="min"]'),
                  max: field.querySelector('[data-thumb="max"]') };
    var box = { min: field.querySelector('[data-box="min"]'),
                max: field.querySelector('[data-box="max"]') };
    if (!fill || !thumb.min || !thumb.max || !box.min || !box.max) return;

    var pct = function (v) { return (100 * v) / max; };

    function paint(lo, hi) {
      fill.style.left = pct(lo) + "%";
      fill.style.width = pct(hi - lo) + "%";
      for (var i = 0; i < ticks.length; i++) {
        var d = +ticks[i].dataset.dte;
        ticks[i].classList.toggle("in-range", d >= lo && d <= hi);
      }
      // The two inputs overlap the full track, so exactly one can receive a click at any
      // given point. DOM order alone would always favour the max thumb — which strands the
      // min thumb permanently once the pair collapses at the right-hand end. Hand the top
      // layer to whichever thumb is in the half of the track where it can still be grabbed.
      var minOnTop = lo > max / 2;
      thumb.min.style.zIndex = minOnTop ? 4 : 2;
      thumb.max.style.zIndex = minOnTop ? 2 : 4;
    }

    function clamp(v) { return Math.min(max, Math.max(1, Math.round(v) || 1)); }

    function commit(which, value, andBox) {
      var lo = clamp(which === "min" ? value : +thumb.min.value);
      var hi = clamp(which === "max" ? value : +thumb.max.value);
      if (which === "min") { hi = Math.max(hi, lo); } else { lo = Math.min(lo, hi); }
      thumb.min.value = lo; thumb.max.value = hi;
      if (andBox) { box.min.value = lo; box.max.value = hi; }
      paint(lo, hi);
    }

    ["min", "max"].forEach(function (k) {
      thumb[k].addEventListener("input", function () { commit(k, +this.value, true); });
      // Typing 4 on the way to 40 must not yank the other thumb, so boxes settle on change.
      box[k].addEventListener("change", function () { commit(k, +this.value, true); });
      box[k].addEventListener("input", function () {
        var v = +this.value;
        if (v >= 1 && v <= max) { thumb[k].value = v; paint(+thumb.min.value, +thumb.max.value); }
      });
    });

    commit("min", +box.min.value, true);
    commit("max", +box.max.value, true);
  }

  function init() {
    var fields = document.querySelectorAll("[data-dte]");
    for (var i = 0; i < fields.length; i++) setup(fields[i]);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }
  document.body && document.body.addEventListener("htmx:afterSwap", init);
})();
