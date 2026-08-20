/* The "agent is driving" overlay — builder, hexagon animation, and the
   click/type bloom. Styling lives beside this in glow.css; glow.html renders
   both on their own so you can tweak the look without running the agent.

   browser.rs registers this as a document-start script on every tab, so it
   runs at the birth of every document — the overlay is part of the first
   paint on every navigation, including ones the agent causes by clicking a
   link. When injected it is handed the stylesheet text in AUTOUSE_CSS and
   adopts it into the document (a constructed stylesheet, which page CSP
   cannot block); glow.html instead links glow.css itself and leaves
   AUTOUSE_CSS undefined.

   Everything it adds is one fixed, pointer-events:none container that takes
   part in no layout, so the page underneath is untouched and the scanner sees
   the same tree with the overlay present or absent. */

(function () {
  // TOP FRAME ONLY. Page.addScriptToEvaluateOnNewDocument runs this at the
  // birth of every same-process frame, not just the tab's main document — so
  // without this guard a same-origin iframe (YouTube's live-chat panel, an
  // embedded checkout form) grows its own full set of edges and hexagons
  // sized to the panel. The glow marks the BROWSER as driven, so it belongs
  // on the viewport alone; cross-origin iframes are separate CDP targets
  // that never get armed, and this makes same-process ones match. The
  // try/catch treats any exotic frame that hides window.top as framed.
  try {
    if (window.self !== window.top) return;
  } catch (e) {
    return;
  }
  if (window.__autouseOverlay) return;

  var CONFIG = {
    inset: 0,           // hexagons start right at the viewport edge
    bandDepth: 65,      // how far inward a crest can reach at full strength
    radius: 3.5,        // centre to corner
    gap: 1.5,           // space between neighbouring hexagons
    rotation: 0,        // 0 = flat top, Math.PI / 6 = pointy top

    baseColor: '180, 230, 255',
    alphaPeak: 0.75,

    patchWidth: 0.25,   // crest width, broad enough to match the CSS edges
    reachCurve: 0.8,
    minScale: 0.15,     // hexagon size at the faint lip of a crest
    cutoff: 0.02,       // below this a hexagon is not drawn at all

    speed: 0.07,        // tuned against the ~7s CSS edge animations
    edgeSpeeds: [1, 0.85, 1.15, 0.9],
    edgePhases: [0, 1.5, 3.14, 4.7],
    travel: 0.4,        // how far a crest slides along its edge
    dance: 0.3,         // breathing depth
    shimmer: 0.03,
    shimmerFreq: 12
  };

  var TAU = Math.PI * 2;
  var STEPS = 32;
  var reduceMotion = false;
  try {
    reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) {}

  // -- the layer ------------------------------------------------------------

  var layer = document.createElement('div');
  layer.className = 'autouse-layer';
  // aria-hidden and inert: decoration must not reach the accessibility tree,
  // which is where the scanner reads the page from.
  layer.setAttribute('aria-hidden', 'true');
  layer.setAttribute('data-autouse', 'overlay');

  var canvas = document.createElement('canvas');
  canvas.className = 'autouse-hex';
  layer.appendChild(canvas);

  ['top', 'bottom', 'left', 'right'].forEach(function (side) {
    var edge = document.createElement('div');
    edge.className = 'autouse-edge autouse-edge-' + side;
    layer.appendChild(edge);
  });

  var box = document.createElement('div');
  box.className = 'autouse-box';
  layer.appendChild(box);

  function installStyle() {
    if (typeof AUTOUSE_CSS !== 'string' || !AUTOUSE_CSS) return;   // glow.html links it instead
    try {
      var sheet = new CSSStyleSheet();
      sheet.replaceSync(AUTOUSE_CSS);
      document.adoptedStyleSheets = document.adoptedStyleSheets.concat(sheet);
    } catch (e) {
      try {
        var el = document.createElement('style');
        el.textContent = AUTOUSE_CSS;
        (document.head || document.documentElement).appendChild(el);
      } catch (e2) {}
    }
  }

  function attach() {
    var host = document.body || document.documentElement;
    if (host && !layer.isConnected) host.appendChild(layer);
    return layer.isConnected;
  }

  // -- hexagons -------------------------------------------------------------

  var ctx = null;
  try { ctx = canvas.getContext('2d'); } catch (e) {}

  var W = 0, H = 0, time = 0, cells = [], palette = [];

  for (var i = 0; i < STEPS; i++) {
    palette.push('rgba(' + CONFIG.baseColor + ', ' +
                 (CONFIG.alphaPeak * (i / (STEPS - 1))).toFixed(3) + ')');
  }

  function hexPath(cx, cy, r, rot) {
    ctx.beginPath();
    for (var i = 0; i < 6; i++) {
      var a = rot + i * Math.PI / 3;
      var x = cx + r * Math.cos(a);
      var y = cy + r * Math.sin(a);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();
  }

  function buildCells() {
    cells = [];
    var r = CONFIG.radius;
    var colStep = 1.5 * r + CONFIG.gap;
    var rowStep = Math.sqrt(3) * r + CONFIG.gap;
    var inner = CONFIG.inset + CONFIG.bandDepth;
    var mid = CONFIG.inset + CONFIG.bandDepth / 2;
    var spanX = Math.max(1, W - mid * 2);
    var spanY = Math.max(1, H - mid * 2);
    var clamp01 = function (v) { return v < 0 ? 0 : (v > 1 ? 1 : v); };

    for (var c = 0; c * colStep < W + colStep; c++) {
      var x = c * colStep + CONFIG.inset;
      var yOffset = (c % 2) * rowStep / 2;

      for (var k = 0; k * rowStep < H + rowStep; k++) {
        var y = k * rowStep + yOffset + CONFIG.inset;
        if (x > W - CONFIG.inset || y > H - CONFIG.inset) continue;

        var dTop = y, dRight = W - x, dBottom = H - y, dLeft = x;
        var edgeDist = Math.min(dTop, dRight, dBottom, dLeft);
        if (edgeDist < CONFIG.inset || edgeDist > inner) continue;

        // Which side this hexagon belongs to, and where along that side.
        var edge, u;
        if (edgeDist === dTop)        { edge = 0; u = clamp01((x - mid) / spanX); }
        else if (edgeDist === dRight) { edge = 1; u = clamp01((y - mid) / spanY); }
        else if (edgeDist === dBottom){ edge = 2; u = clamp01((x - mid) / spanX); }
        else                          { edge = 3; u = clamp01((y - mid) / spanY); }

        cells.push({
          x: x, y: y, edge: edge, u: u,
          depth: (edgeDist - CONFIG.inset) / CONFIG.bandDepth
        });
      }
    }
  }

  // Each side gets its own crest: where it sits now, and how tall it is.
  function edgeStates() {
    var out = [];
    for (var e = 0; e < 4; e++) {
      var w = TAU * CONFIG.speed * CONFIG.edgeSpeeds[e];
      var o = CONFIG.edgePhases[e];
      out.push({
        centre: 0.5 + CONFIG.travel * Math.sin(time * w + o),
        breathe: 1 - CONFIG.dance * (0.5 - 0.5 * Math.sin(time * w * 1.63 + o * 1.9))
      });
    }
    return out;
  }

  function resize() {
    if (!ctx) return;
    var dpr = window.devicePixelRatio || 1;
    W = layer.clientWidth || window.innerWidth || 0;
    H = layer.clientHeight || window.innerHeight || 0;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    buildCells();
    draw();
  }

  function draw() {
    if (!ctx) return;
    ctx.clearRect(0, 0, W, H);
    var states = edgeStates();
    var pw2 = 2 * CONFIG.patchWidth * CONFIG.patchWidth;

    for (var i = 0; i < cells.length; i++) {
      var cell = cells[i];
      var state = states[cell.edge];

      // Crest strength at this point along the side.
      var d = cell.u - state.centre;
      var v = Math.exp(-(d * d) / pw2) * state.breathe;
      v *= 1 + CONFIG.shimmer * Math.sin(cell.u * CONFIG.shimmerFreq + time * 1.6);
      if (v > 1) v = 1;
      if (v < CONFIG.cutoff) continue;

      // How far inward the crest reaches here.
      var reach = Math.pow(v, CONFIG.reachCurve);
      if (cell.depth > reach) continue;

      var intensity = v * Math.cos((cell.depth / reach) * Math.PI / 2);
      if (intensity < CONFIG.cutoff) continue;

      var scale = CONFIG.minScale + (1 - CONFIG.minScale) * intensity;
      ctx.fillStyle = palette[Math.round(intensity * (STEPS - 1))];
      hexPath(cell.x, cell.y, CONFIG.radius * scale, CONFIG.rotation);
      ctx.fill();
    }
  }

  var lastTime = 0;
  var lastW = 0, lastH = 0;

  function tick(now) {
    var dt = Math.min((now - lastTime) / 1000, 0.1);
    lastTime = now;
    time += dt;
    // A page that rebuilds its body (SPA route change, framework hydration)
    // can take the layer with it; re-attaching costs one property read.
    if (!layer.isConnected) attach();
    if (layer.clientWidth !== lastW || layer.clientHeight !== lastH) {
      lastW = layer.clientWidth;
      lastH = layer.clientHeight;
      resize();
    }
    draw();
    requestAnimationFrame(tick);
  }

  // -- the click / type bloom ----------------------------------------------

  // Called with the CSS-pixel rect the controller acted on. Viewport
  // coordinates and a fixed-position box, so no dpr conversion exists to get
  // wrong — the bloom lands exactly where the click did.
  //
  // A COMPACT, CENTRED point of light — not the element's outline. Sizing the
  // box to the whole rect made a big control (a 260px "hold" button, a wide
  // banner) glow edge-to-edge, which reads as "the whole thing lit up" rather
  // than "here". Instead the bloom is a small circle centred on the element:
  // its diameter tracks the SMALLER side so a tiny target still gets a visible
  // dot, but it is clamped so a large one stays a focused centre-glow.
  window.__autouseBox = function (x, y, w, h) {
    var cx = x + w / 2, cy = y + h / 2;
    // ~70% of the shorter side, held between 34px (always visible on a small
    // icon) and 96px (never spills across a large element's face).
    var size = Math.min(w, h) * 0.7;
    size = Math.max(34, Math.min(96, size));
    var r = size / 2;
    // setProperty with 'important' so a page's own stylesheet cannot pin the
    // bloom somewhere else; glow.css deliberately leaves these four
    // unimportant so this wins.
    box.style.setProperty('left', (cx - r) + 'px', 'important');
    box.style.setProperty('top', (cy - r) + 'px', 'important');
    box.style.setProperty('width', size + 'px', 'important');
    box.style.setProperty('height', size + 'px', 'important');
    box.classList.remove('autouse-box-on');
    void box.offsetWidth;                 // restart the animation
    box.classList.add('autouse-box-on');
    return true;
  };

  window.__autouseBoxHide = function () {
    box.classList.remove('autouse-box-on');
    return true;
  };

  // -- "did anything actually move?" ---------------------------------------

  // Where the surfaces under a point currently sit, as [innerX, innerY,
  // pageX, pageY]. The scroll tool reads it before and after a wheel: same
  // numbers means nothing moved, which is a fact the agent cannot otherwise
  // recover — it is never shown the previous screenshot.
  //
  // Both the nearest scroller AND the document are reported because a wheel
  // chains: a panel already at its end passes the scroll to the page, and
  // that is still something moving.
  window.__autouseScrollProbe = function (x, y) {
    try {
      var doc = document.scrollingElement || document.documentElement;
      var inner = null;
      // The overlay is pointer-events:none, so this returns the page's own
      // element rather than anything of ours.
      for (var n = document.elementFromPoint(x, y); n && n !== doc; n = n.parentElement) {
        var cs = getComputedStyle(n);
        var canY = /^(auto|scroll|overlay)$/.test(cs.overflowY) &&
                   n.scrollHeight > n.clientHeight + 1;
        var canX = /^(auto|scroll|overlay)$/.test(cs.overflowX) &&
                   n.scrollWidth > n.clientWidth + 1;
        if (canY || canX) { inner = n; break; }
      }
      return [
        inner ? Math.round(inner.scrollLeft) : -1,
        inner ? Math.round(inner.scrollTop) : -1,
        Math.round(doc.scrollLeft),
        Math.round(doc.scrollTop)
      ];
    } catch (e) {
      return null;
    }
  };

  // -- go -------------------------------------------------------------------

  window.__autouseOverlay = 1;
  installStyle();

  if (!attach()) {
    // document-start: <body> does not exist yet. Take the first chance the
    // parser gives us rather than waiting for DOMContentLoaded, which on a
    // slow page is a visible delay.
    try {
      var mo = new MutationObserver(function () {
        if (attach()) { mo.disconnect(); resize(); }
      });
      mo.observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) {}
    document.addEventListener('DOMContentLoaded', function () {
      attach(); resize();
    }, { once: true });
  }

  window.addEventListener('resize', resize);
  resize();
  if (!reduceMotion && CONFIG.speed !== 0) {
    requestAnimationFrame(function (now) { lastTime = now; tick(now); });
  }
})();
