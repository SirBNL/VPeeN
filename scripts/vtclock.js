/* VPeeN deterministic GIF capture layer — injected BEFORE the app loads.
 *
 * 1. Virtual clock: Date / performance.now / setTimeout / setInterval /
 *    requestAnimationFrame are replaced with a clock that only advances
 *    when the capture driver calls __tick(dt).  Toast lifetimes, the
 *    session counter, the status-message rotator and the poll loop all
 *    follow the *scripted* timeline, so nothing drifts against the GIF.
 * 2. Animation stepper: every CSS transition / animation is paused and
 *    re-positioned frame-by-frame (__stepAnims) — sheet slides, toasts,
 *    hover fades, the ring sweep and the stat ticks play back perfectly
 *    smooth at the GIF frame rate, regardless of how slow each real
 *    screenshot is.  The aurora blobs stay parked (file-size relief).
 * 3. Premium cursor: a bespoke "aurora dart" pointer with a lerped glow
 *    aura, a velocity comet trail and layered click ripples.  All
 *    geometry is computed in Python every frame and applied here via
 *    __cursor.render(state) — deterministic, no wall-clock dependency.
 */
(function () {
  if (window.__vt) return;

  /* ---------------------------------------------------- virtual clock */
  var VT = (window.__vt = { now: 0, timers: [], seq: 1 });
  var OrigDate = Date;

  class VDate extends OrigDate {
    constructor(...args) {
      if (args.length === 0) super(VT.now * 1000);
      else super(...args);
    }
    static now() { return Math.floor(VT.now * 1000); }
  }
  try { window.Date = VDate; } catch (e) { /* keep native */ }

  try { window.performance.now = function () { return VT.now * 1000; }; }
  catch (e) { /* keep native */ }

  function rest(args, n) { return Array.prototype.slice.call(args, n); }
  function setVTimeout(fn, ms) {
    var id = VT.seq++;
    VT.timers.push({ id: id, at: VT.now + Math.max(0, +ms || 0) / 1000,
                     fn: fn, args: rest(arguments, 2), every: 0 });
    return id;
  }
  function setVInterval(fn, ms) {
    var id = VT.seq++;
    var s = Math.max(0, +ms || 0) / 1000;
    VT.timers.push({ id: id, at: VT.now + s, fn: fn,
                     args: rest(arguments, 2), every: s });
    return id;
  }
  function clearV(id) {
    id = +id;
    for (var i = 0; i < VT.timers.length; i++) {
      if (VT.timers[i].id === id) { VT.timers.splice(i, 1); return; }
    }
  }
  window.setTimeout = setVTimeout;
  window.clearTimeout = clearV;
  window.setInterval = setVInterval;
  window.clearInterval = clearV;
  window.requestAnimationFrame = function (fn) { return setVTimeout(fn, 16); };
  window.cancelAnimationFrame = clearV;

  window.__tick = function (dt) {
    VT.now += dt;
    for (var guard = 0; guard < 20000; guard++) {
      var best = -1;
      for (var i = 0; i < VT.timers.length; i++) {
        if (VT.timers[i].at <= VT.now &&
            (best < 0 || VT.timers[i].at < VT.timers[best].at)) best = i;
      }
      if (best < 0) break;
      var t = VT.timers.splice(best, 1)[0];
      if (t.every) {
        VT.timers.push({ id: t.id, at: VT.now + t.every, fn: t.fn,
                         args: t.args, every: t.every });
      }
      try { t.fn.apply(null, t.args); } catch (e) { /* keep ticking */ }
    }
    return VT.now;
  };

  /* ---------------------------------------------- animation stepper */
  var REG = new Map();
  var PARK = { drift1: 9000, drift2: 21000, drift3: 14000 };
  window.__stepAnims = function () {
    var anims;
    try { anims = document.getAnimations(); } catch (e) { return; }
    var seen = new Set();
    for (var i = 0; i < anims.length; i++) {
      var a = anims[i];
      seen.add(a);
      var info = REG.get(a);
      if (!info) {
        info = { base: VT.now, park: null };
        try {
          var nm = a.animationName || "";
          if (PARK.hasOwnProperty(nm)) info.park = PARK[nm];
          a.currentTime = info.park != null ? info.park : 0;
          a.pause();
        } catch (e) { /* not steppable */ }
        REG.set(a, info);
      }
      if (info.park != null) continue;
      try { a.currentTime = Math.max(0, (VT.now - info.base) * 1000); }
      catch (e) { /* finished */ }
    }
    REG.forEach(function (v, a) { if (!seen.has(a)) REG.delete(a); });
  };

  /* ------------------------------------------------- premium cursor */
  var CURSOR_CSS = [
    "* { cursor: none !important; }",
    "#vx-layer { position: fixed; left: 0; top: 0; width: 0; height: 0;",
    "  z-index: 2147483647; pointer-events: none; }",
    "#vx-aura { position: fixed; left: 0; top: 0; width: 168px; height: 168px;",
    "  margin: -84px 0 0 -84px; border-radius: 50%;",
    "  background: radial-gradient(circle,",
    "    rgba(52,240,185,.40) 0%, rgba(56,189,248,.18) 38%,",
    "    rgba(129,140,248,.08) 55%, rgba(129,140,248,0) 70%);",
    "  filter: blur(4px); will-change: transform, opacity; }",
    ".vx-ghost { position: fixed; left: 0; top: 0; width: 16px; height: 16px;",
    "  margin: -8px 0 0 -8px; border-radius: 50%; opacity: 0;",
    "  background: radial-gradient(circle, rgba(240,255,250,.98),",
    "    rgba(96,250,209,.7) 42%, rgba(96,250,209,0) 70%);",
    "  filter: blur(.5px); will-change: transform, opacity; }",
    "#vx-ptr { position: fixed; left: 0; top: 0; width: 0; height: 0;",
    "  transform-origin: 0 0; will-change: transform;",
    "  filter: drop-shadow(0 8px 18px rgba(2,10,25,.62))",
    "          drop-shadow(0 0 20px rgba(52,240,185,.5))",
    "          drop-shadow(0 0 3px rgba(255,255,255,.5)); }",
    "#vx-ptr svg { display: block; position: absolute;",
    "  left: -2.71px; top: -1.63px; width: 38px; height: 57.6px; }",
    ".vx-fx { position: fixed; left: 0; top: 0; width: 0; height: 0;",
    "  opacity: 0; }",
    ".fx-ring { position: absolute; left: 0; top: 0; border-radius: 50%;",
    "  will-change: transform, opacity; }",
    ".fx-a { width: 96px; height: 96px; margin: -48px 0 0 -48px;",
    "  border: 4px solid rgba(118,255,220,1);",
    "  box-shadow: 0 0 52px rgba(52,240,185,.75),",
    "              0 0 18px rgba(118,255,220,.95),",
    "              inset 0 0 28px rgba(52,240,185,.5); }",
    ".fx-b { width: 62px; height: 62px; margin: -31px 0 0 -31px;",
    "  border: 3px solid rgba(154,255,228,.95);",
    "  box-shadow: 0 0 30px rgba(52,240,185,.6),",
    "              inset 0 0 16px rgba(52,240,185,.4); }",
    ".fx-flash { position: absolute; left: 0; top: 0; width: 32px; height: 32px;",
    "  margin: -16px 0 0 -16px; border-radius: 50%;",
    "  background: radial-gradient(circle, rgba(250,255,252,1),",
    "    rgba(118,255,220,.9) 42%, rgba(52,240,185,.3) 66%,",
    "    rgba(52,240,185,0) 82%);",
    "  will-change: transform, opacity; }"
  ].join("\n");

  var PTR_SVG = [
    '<svg viewBox="0 0 14 21.2" fill="none" xmlns="http://www.w3.org/2000/svg">',
    '<defs>',
    '<linearGradient id="vxg" x1="0" y1="0" x2=".7" y2="1">',
    '<stop offset="0" stop-color="#ffffff"/>',
    '<stop offset=".45" stop-color="#dff9f0"/>',
    '<stop offset="1" stop-color="#6fe3bd"/>',
    '</linearGradient>',
    '<linearGradient id="vxa" x1="0" y1="0" x2="0" y2="1">',
    '<stop offset="0" stop-color="#7ef0cd"/>',
    '<stop offset="1" stop-color="#9fd8ff" stop-opacity=".15"/>',
    '</linearGradient>',
    '</defs>',
    '<path d="M1 .6 L1 17.2 C1 18.05 2.05 18.4 2.6 17.75 L5.35 14.5',
    'L7.55 20.7 C7.8 21.35 8.6 21.65 9.2 21.35 L10.55 20.7',
    'C11.15 20.4 11.4 19.65 11.1 19.1 L8.9 14.1 L12.85 13.75',
    'C13.7 13.65 14.05 12.6 13.4 12.05 L2.1 .35 C1.5 -.2 1 0 1 .6 Z"',
    'fill="url(#vxg)" stroke="#081522" stroke-width="1.25"',
    'stroke-linejoin="round" stroke-linecap="round"/>',
    '<path d="M2.9 2.3 L11.9 12.1" stroke="#2de3a7" stroke-width=".85"',
    'stroke-linecap="round" opacity=".9"/>',
    '<path d="M2.25 2.8 L2.25 15.4" stroke="url(#vxa)" stroke-width=".7"',
    'stroke-linecap="round" opacity=".85"/>',
    '</svg>'
  ].join(" ");

  var L = null, ptr = null, aura = null;
  var ghosts = [], fxs = [];

  function build() {
    if (L) return;
    var st = document.createElement("style");
    st.textContent = CURSOR_CSS;
    document.documentElement.appendChild(st);

    L = document.createElement("div");
    L.id = "vx-layer";
    var html = ['<div id="vx-aura"></div>'];
    for (var i = 0; i < 8; i++) {
      html.push('<div class="vx-ghost" id="vx-g' + i + '"></div>');
    }
    for (var i = 0; i < 3; i++) {
      html.push('<div class="vx-fx" id="vx-fx' + i + '">' +
                '<div class="fx-ring fx-a"></div>' +
                '<div class="fx-ring fx-b"></div>' +
                '<div class="fx-flash"></div></div>');
    }
    html.push('<div id="vx-ptr">' + PTR_SVG + "</div>");
    L.innerHTML = html.join("");
    document.documentElement.appendChild(L);

    ptr = L.querySelector("#vx-ptr");
    aura = L.querySelector("#vx-aura");
    for (var i = 0; i < 8; i++) ghosts.push(L.querySelector("#vx-g" + i));
    for (var i = 0; i < 3; i++) {
      var el = L.querySelector("#vx-fx" + i);
      fxs.push({ el: el, a: el.querySelector(".fx-a"),
                 b: el.querySelector(".fx-b"),
                 f: el.querySelector(".fx-flash") });
    }
  }

  function ring(el, x, y, scale, op, bw) {
    el.style.transform = "translate3d(" + x + "px," + y + "px,0) " +
      "scale(" + scale.toFixed(4) + ")";
    el.style.opacity = op.toFixed(3);
    if (bw != null) el.style.borderWidth = bw.toFixed(2) + "px";
  }

  window.__cursor = {
    render: function (st) {
      build();
      if (st.o != null) L.style.opacity = String(st.o);

      ptr.style.transform = "translate3d(" + st.x.toFixed(2) + "px," +
        st.y.toFixed(2) + "px,0) rotate(" + (st.tilt || 0).toFixed(2) +
        "deg) scale(" + (st.ps || 1).toFixed(4) + ")";

      if (st.ax != null) {
        aura.style.transform = "translate3d(" + st.ax.toFixed(2) + "px," +
          st.ay.toFixed(2) + "px,0) scale(" + (st.ak || 1).toFixed(4) + ")";
        aura.style.opacity = String(st.ao == null ? 1 : st.ao);
      }

      var g = st.g || [];
      for (var i = 0; i < ghosts.length; i++) {
        var d = g[i];
        if (!d || !d[2]) { ghosts[i].style.opacity = "0"; continue; }
        ghosts[i].style.opacity = d[2].toFixed(3);
        ghosts[i].style.transform = "translate3d(" + d[0].toFixed(2) + "px," +
          d[1].toFixed(2) + "px,0) scale(" + d[3].toFixed(3) + ")";
      }

      var f = st.fx || [];
      for (var i = 0; i < fxs.length; i++) {
        var e = f[i], s = fxs[i];
        if (!e) { s.el.style.opacity = "0"; continue; }
        s.el.style.opacity = "1";
        if (e.as != null) ring(s.a, e.x, e.y, e.as, e.ao, e.ab);
        else s.a.style.opacity = "0";
        if (e.bs != null) ring(s.b, e.x, e.y, e.bs, e.bo, e.bb);
        else s.b.style.opacity = "0";
        if (e.fs != null) {
          s.f.style.transform = "translate3d(" + e.x + "px," + e.y +
            "px,0) scale(" + e.fs.toFixed(4) + ")";
          s.f.style.opacity = String(e.fo);
        } else s.f.style.opacity = "0";
      }
    }
  };

  /* one call per captured frame */
  window.__frame = function (st) {
    window.__stepAnims();
    window.__cursor.render(st);
  };

  /* clipboard always succeeds — the copy-IP toast must demo correctly */
  try {
    if (navigator.clipboard) {
      navigator.clipboard.writeText = function () { return Promise.resolve(); };
    }
  } catch (e) { /* ignore */ }
})();
