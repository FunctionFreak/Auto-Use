// =====================================================================
// Top-left SCREEN OVERLAYS — the web globe + the shell terminal SWAP IN over the
// live screenshot while a web / shell command runs (the screenshot fades out,
// the globe or terminal fades in; on finish it swaps back). Self-contained here.
// Driven by the backend's existing pywebview hooks:
//     window.webSearchStart() / window.webSearchEnd()        — globe (Three.js earth)
//     window.shellStart() / window.shellResult() / window.shellEnd()  — shell terminal
// DOM lives in container/top_left/top_left.html (#globePanel/#mainGlobeContainer
// and #shellPanel/#shellTerminalContainer); globe_shell.css cross-fades them with
// the screenshot. Elements are resolved lazily (top_left.html is fetch-injected),
// so this file's load order doesn't matter.
// =====================================================================
(function () {
    'use strict';

    // Fade the live screenshot out while an overlay (globe/shell) is showing, and
    // back in when it's gone (.overlay-active on the top-left zone).
    const fadeScreenshot = (hide) => {
        const z = document.getElementById('zoneTopLeft');
        if (z) z.classList.toggle('overlay-active', hide);
    };

    /* ============================================================
       WEB GLOBE  (Three.js earth — swaps in over the screenshot)
       ============================================================ */
    const setGlobePanel = (on) => {
        const p = document.getElementById('globePanel');
        if (p) p.classList.toggle('is-active', on);
    };

    let mainGlobeContainer = null;
    let globeInitialized = false;
    let globeScene, globeCamera, globeRenderer, globeEarth, globeNetworkGroup;
    let globeParticles, globeLineMesh, globeActivePackets;
    let globeAnimationId = null;

    const initMainGlobe = () => {
        if (!mainGlobeContainer) mainGlobeContainer = document.getElementById('mainGlobeContainer');
        if (globeInitialized || !mainGlobeContainer || typeof THREE === 'undefined') return;
        globeInitialized = true;

        // Get container dimensions for responsive sizing
        const containerRect = mainGlobeContainer.getBoundingClientRect();
        const size = Math.min(containerRect.width, containerRect.height) * 0.95;

        // Scene setup - transparent background
        globeScene = new THREE.Scene();

        globeCamera = new THREE.PerspectiveCamera(45, 1, 1, 1000);
        globeCamera.position.z = 12;

        globeRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        globeRenderer.setClearColor(0x000000, 0);
        globeRenderer.setSize(size, size);
        globeRenderer.setPixelRatio(window.devicePixelRatio);
        mainGlobeContainer.appendChild(globeRenderer.domElement);

        // Texture generation helpers
        const getX = (lon) => (lon + 180) * (4096 / 360);
        const getY = (lat) => ((-lat) + 90) * (2048 / 180);

        const drawContinentsPath = (ctx) => {
            ctx.beginPath();
            const drawPoly = (coords) => {
                ctx.moveTo(getX(coords[0][0]), getY(coords[0][1]));
                for (let i = 1; i < coords.length; i++) {
                    ctx.lineTo(getX(coords[i][0]), getY(coords[i][1]));
                }
            };
            drawPoly([[-77, 8], [-75, 11], [-60, 10], [-50, 5], [-35, -5], [-35, -10], [-39, -20], [-40, -30], [-55, -55], [-70, -55], [-75, -50], [-73, -40], [-71, -30], [-75, -20], [-81, -5], [-77, 8]]);
            drawPoly([[-165, 65], [-120, 70], [-90, 75], [-70, 70], [-60, 60], [-55, 52], [-75, 35], [-80, 25], [-82, 9], [-95, 18], [-105, 20], [-125, 35], [-125, 45], [-130, 50], [-165, 65]]);
            drawPoly([[-50, 60], [-40, 65], [-30, 80], [-60, 80], [-50, 60]]);
            drawPoly([[-15, 35], [10, 37], [30, 31], [40, 15], [51, 11], [45, -10], [40, -15], [35, -30], [20, -35], [10, -5], [5, 5], [-10, 5], [-17, 15], [-15, 35]]);
            drawPoly([[43, -25], [50, -15], [49, -12], [44, -22]]);
            drawPoly([[-10, 36], [-9, 43], [0, 50], [10, 55], [25, 70], [40, 65], [35, 45], [25, 35], [15, 40], [10, 45], [5, 42], [-10, 36]]);
            drawPoly([[-5, 50], [2, 51], [0, 58], [-6, 56]]);
            drawPoly([[40, 65], [60, 75], [100, 75], [170, 70], [140, 50], [130, 40], [120, 30], [120, 20], [110, 10], [100, 15], [90, 22], [80, 5], [70, 10], [60, 25], [50, 30], [40, 45], [40, 65]]);
            drawPoly([[130, 32], [138, 36], [142, 40], [140, 45], [135, 35]]);
            drawPoly([[100, 0], [110, -5], [140, -5], [150, -10], [130, 0]]);
            drawPoly([[113, -25], [130, -12], [145, -10], [153, -25], [150, -38], [135, -35], [115, -35], [113, -25]]);
            drawPoly([[166, -45], [174, -35], [178, -38], [168, -47]]);
            ctx.closePath();
        };

        // Color texture
        const createColorTexture = () => {
            const canvas = document.createElement('canvas');
            canvas.width = 4096; canvas.height = 2048;
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(0, 0, 4096, 2048);
            ctx.shadowColor = 'rgba(0, 0, 0, 0.4)';
            ctx.shadowBlur = 30;
            ctx.fillStyle = '#dcdcdc';
            ctx.strokeStyle = '#555555';
            ctx.lineWidth = 4;
            ctx.lineJoin = 'round';
            drawContinentsPath(ctx);
            ctx.fill();
            ctx.stroke();
            ctx.globalCompositeOperation = 'source-atop';
            for (let i = 0; i < 2000; i++) {
                const x = Math.random() * 4096;
                const y = Math.random() * 2048;
                const r = 5 + Math.random() * 20;
                ctx.fillStyle = 'rgba(200, 200, 200, 0.1)';
                ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
            }
            ctx.shadowColor = 'transparent';
            ctx.strokeStyle = '#cccccc';
            ctx.lineWidth = 1;
            ctx.beginPath();
            for (let x = 0; x < 4096; x += 60) { ctx.moveTo(x, 0); ctx.lineTo(x, 2048); }
            for (let y = 0; y < 2048; y += 60) { ctx.moveTo(0, y); ctx.lineTo(4096, y); }
            ctx.stroke();
            return new THREE.CanvasTexture(canvas);
        };

        // Height texture
        const createHeightTexture = () => {
            const canvas = document.createElement('canvas');
            canvas.width = 4096; canvas.height = 2048;
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = '#000000';
            ctx.fillRect(0, 0, 4096, 2048);
            ctx.save();
            drawContinentsPath(ctx);
            ctx.clip();
            ctx.fillStyle = '#808080';
            ctx.fillRect(0, 0, 4096, 2048);
            for (let i = 0; i < 10000; i++) {
                const x = Math.random() * 4096;
                const y = Math.random() * 2048;
                const radius = 5 + Math.random() * 30;
                const shade = Math.floor(100 + Math.random() * 155);
                const grad = ctx.createRadialGradient(x, y, 0, x, y, radius);
                grad.addColorStop(0, `rgba(${shade}, ${shade}, ${shade}, 0.5)`);
                grad.addColorStop(1, `rgba(${shade}, ${shade}, ${shade}, 0)`);
                ctx.fillStyle = grad;
                ctx.beginPath();
                ctx.arc(x, y, radius, 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.strokeStyle = '#e0e0e0';
            ctx.lineWidth = 2;
            drawContinentsPath(ctx);
            ctx.stroke();
            ctx.restore();
            return new THREE.CanvasTexture(canvas);
        };

        // Earth
        const earthGeo = new THREE.SphereGeometry(4, 128, 128);
        const earthMat = new THREE.MeshPhongMaterial({
            map: createColorTexture(),
            displacementMap: createHeightTexture(),
            displacementScale: 0.5,
            displacementBias: 0,
            color: 0xffffff,
            specular: 0x333333,
            shininess: 8
        });
        globeEarth = new THREE.Mesh(earthGeo, earthMat);
        globeScene.add(globeEarth);

        // Atmosphere
        const atmGeo = new THREE.SphereGeometry(4.2, 64, 64);
        const atmMat = new THREE.MeshBasicMaterial({
            color: 0x888888,
            transparent: true,
            opacity: 0.05,
            side: THREE.BackSide
        });
        const atmosphere = new THREE.Mesh(atmGeo, atmMat);
        globeScene.add(atmosphere);

        // Network
        const particlesCount = 100;
        const connectionDistance = 2.5;
        const sphereRadius = 4.4;

        globeNetworkGroup = new THREE.Group();
        globeScene.add(globeNetworkGroup);

        const packetColors = [0x222222, 0x333333, 0x111111];
        const particleGeo = new THREE.SphereGeometry(0.04, 8, 8);
        globeParticles = [];

        for (let i = 0; i < particlesCount; i++) {
            const phi = Math.acos(-1 + (2 * i) / particlesCount);
            const theta = Math.sqrt(particlesCount * Math.PI) * phi;
            const greyVal = 0.5 + Math.random() * 0.3;
            const mat = new THREE.MeshBasicMaterial({ color: new THREE.Color(greyVal, greyVal, greyVal) });
            const mesh = new THREE.Mesh(particleGeo, mat);
            mesh.position.setFromSphericalCoords(sphereRadius, phi, theta);
            mesh.position.x += (Math.random() - 0.5) * 0.2;
            mesh.position.y += (Math.random() - 0.5) * 0.2;
            mesh.position.z += (Math.random() - 0.5) * 0.2;
            mesh.userData = {
                velocity: new THREE.Vector3((Math.random() - 0.5) * 0.005, (Math.random() - 0.5) * 0.005, (Math.random() - 0.5) * 0.005),
                packetColor: packetColors[Math.floor(Math.random() * packetColors.length)]
            };
            globeNetworkGroup.add(mesh);
            globeParticles.push(mesh);
        }

        const lineMaterial = new THREE.LineBasicMaterial({ color: 0x999999, transparent: true, opacity: 0.2 });
        globeLineMesh = new THREE.LineSegments(new THREE.BufferGeometry(), lineMaterial);
        globeNetworkGroup.add(globeLineMesh);

        // Packets
        const packetGeo = new THREE.BufferGeometry();
        const packetMat = new THREE.PointsMaterial({
            size: 0.16,
            vertexColors: true,
            transparent: true,
            opacity: 0.9,
            map: (() => {
                const canvas = document.createElement('canvas');
                canvas.width = 32; canvas.height = 32;
                const ctx = canvas.getContext('2d');
                ctx.beginPath();
                ctx.arc(16, 16, 14, 0, Math.PI * 2);
                ctx.fillStyle = 'white';
                ctx.fill();
                return new THREE.CanvasTexture(canvas);
            })()
        });
        const packetSystem = new THREE.Points(packetGeo, packetMat);
        globeNetworkGroup.add(packetSystem);
        globeActivePackets = [];

        // Lighting
        const ambientLight = new THREE.AmbientLight(0xffffff, 0.7);
        globeScene.add(ambientLight);
        const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
        dirLight.position.set(20, 10, 20);
        globeScene.add(dirLight);
        const rimLight = new THREE.DirectionalLight(0xeeeeee, 0.3);
        rimLight.position.set(-10, 10, -20);
        globeScene.add(rimLight);

        // Animation loop
        const animateGlobe = () => {
            globeAnimationId = requestAnimationFrame(animateGlobe);

            globeEarth.rotation.y += 0.003;
            globeNetworkGroup.rotation.y += 0.0032;

            const linePositions = [];
            const connections = [];

            globeParticles.forEach((p) => {
                p.position.add(p.userData.velocity);
                p.position.normalize().multiplyScalar(sphereRadius);
            });

            for (let i = 0; i < globeParticles.length; i++) {
                for (let j = i + 1; j < globeParticles.length; j++) {
                    const dist = globeParticles[i].position.distanceTo(globeParticles[j].position);
                    if (dist < connectionDistance) {
                        linePositions.push(
                            globeParticles[i].position.x, globeParticles[i].position.y, globeParticles[i].position.z,
                            globeParticles[j].position.x, globeParticles[j].position.y, globeParticles[j].position.z
                        );
                        connections.push({ start: globeParticles[i].position, end: globeParticles[j].position, color: globeParticles[i].userData.packetColor });
                    }
                }
            }

            globeLineMesh.geometry.dispose();
            const lineGeo = new THREE.BufferGeometry();
            lineGeo.setAttribute('position', new THREE.Float32BufferAttribute(linePositions, 3));
            globeLineMesh.geometry = lineGeo;

            for (let k = 0; k < 5; k++) {
                if (Math.random() > 0.5 && connections.length > 0) {
                    const route = connections[Math.floor(Math.random() * connections.length)];
                    globeActivePackets.push({ start: route.start, end: route.end, progress: 0, speed: 0.01 + Math.random() * 0.02, color: new THREE.Color(route.color) });
                }
            }

            const packetPositions = [];
            const packetColorsArr = [];

            for (let i = globeActivePackets.length - 1; i >= 0; i--) {
                const pkt = globeActivePackets[i];
                pkt.progress += pkt.speed;
                if (pkt.progress >= 1) { globeActivePackets.splice(i, 1); continue; }
                const x = THREE.MathUtils.lerp(pkt.start.x, pkt.end.x, pkt.progress);
                const y = THREE.MathUtils.lerp(pkt.start.y, pkt.end.y, pkt.progress);
                const z = THREE.MathUtils.lerp(pkt.start.z, pkt.end.z, pkt.progress);
                packetPositions.push(x, y, z);
                packetColorsArr.push(pkt.color.r, pkt.color.g, pkt.color.b);
            }

            packetGeo.setAttribute('position', new THREE.Float32BufferAttribute(packetPositions, 3));
            packetGeo.setAttribute('color', new THREE.Float32BufferAttribute(packetColorsArr, 3));

            globeRenderer.render(globeScene, globeCamera);
        };

        animateGlobe();

        // Resize handler for responsive globe
        const handleGlobeResize = () => {
            if (!globeRenderer || !globeCamera || !mainGlobeContainer) return;
            const r = mainGlobeContainer.getBoundingClientRect();
            const newSize = Math.min(r.width, r.height) * 0.95;
            globeRenderer.setSize(newSize, newSize);
            globeCamera.updateProjectionMatrix();
        };
        window.addEventListener('resize', handleGlobeResize);
    };

    // Web search — the globe swaps in over the screenshot (screenshot fades out).
    window.webSearchStart = () => {
        fadeScreenshot(true);
        setGlobePanel(true);                  // fade the panel in first…
        try { initMainGlobe(); } catch (e) { /* WebGL unavailable — panel still shows */ }
        // Resize the renderer to the box once the fade-in starts.
        setTimeout(() => {
            if (globeRenderer && globeCamera && mainGlobeContainer) {
                const r = mainGlobeContainer.getBoundingClientRect();
                const newSize = Math.min(r.width, r.height) * 0.95;
                globeRenderer.setSize(newSize, newSize);
                globeCamera.updateProjectionMatrix();
            }
        }, 100);
    };
    window.webSearchEnd = () => { setGlobePanel(false); fadeScreenshot(false); };

    /* ============================================================
       SHELL TERMINAL  (coder-card style — transparent `>` cmd + dot loader,
       then an L-connector down to the output that comes back)
       ============================================================ */
    // Resolved lazily (top_left.html is fetch-injected) — re-query on each event.
    let shellTerminalContainer = null;
    let shellCmdText = null;
    let shellLoader = null;
    let shellOutLine = null;
    let shellOutText = null;

    const resolveShellEls = () => {
        shellTerminalContainer = document.getElementById('shellTerminalContainer');
        shellCmdText = document.getElementById('shellCmdText');
        shellLoader = document.getElementById('shellLoader');
        shellOutLine = document.getElementById('shellOutLine');
        shellOutText = document.getElementById('shellOutText');
    };

    let shellCmdStream = null;     // active command stream handle ({ stop })
    let shellOutStream = null;     // active output stream handle
    let shellCmdFull = '';         // full command text (to force-complete on result)

    // Smooth char-by-char streamer — a port of coder_card.js makeLineStreamer's
    // per-letter fade: each character is appended as a span that fades opacity
    // 0→1, a new one every SH_STAGGER ms, so the leading edge is a soft fade-in
    // wave (NOT a chunky substring jump). On done the spans are flattened back to
    // plain text so the caller can apply the shimmer cleanly. Returns { stop }.
    // FAST, coder-paced: reveal SH_STEP chars per SH_STAGGER tick (browser timers
    // clamp to a few ms, so batching is how you get real speed), each char fading
    // in over SH_CHAR_FADE — same per-letter fade mechanic as coder_card.js, same
    // brisk cadence (STEP 5 / 4ms / 30ms), not the earlier sluggish 1-char/22ms.
    const SH_STEP = 5;          // chars revealed per tick
    const SH_STAGGER = 4;       // ms between ticks
    const SH_CHAR_FADE = 30;    // ms opacity 0→1 per letter
    const streamChars = (element, text, onDone) => {
        element.textContent = '';
        const chars = Array.from(String(text));   // codepoint-safe (emoji/surrogates)
        let i = 0;
        let timer = null;
        const tick = () => {
            for (let n = 0; n < SH_STEP; n++) {
                if (i >= chars.length) {
                    element.textContent = element.textContent;   // flatten spans → plain text
                    timer = null;
                    if (onDone) onDone();
                    return;
                }
                const span = document.createElement('span');
                span.className = 'sh-char';
                span.textContent = chars[i];
                span.style.opacity = '0';
                span.style.transition = 'opacity ' + SH_CHAR_FADE + 'ms ease-out';
                element.appendChild(span);
                // First char shows instantly (no empty flash); the rest fade in.
                if (i === 0) span.style.opacity = '1';
                else requestAnimationFrame(() => { span.style.opacity = '1'; });
                i++;
            }
            timer = setTimeout(tick, SH_STAGGER);
        };
        tick();
        return { stop: () => { if (timer) { clearTimeout(timer); timer = null; } } };
    };

    const resetShellTerminal = () => {
        resolveShellEls();
        if (shellCmdStream) { shellCmdStream.stop(); shellCmdStream = null; }
        if (shellOutStream) { shellOutStream.stop(); shellOutStream = null; }
        shellCmdFull = '';
        if (shellCmdText) { shellCmdText.textContent = ''; shellCmdText.classList.remove('sh-shimmer'); }
        if (shellOutText) { shellOutText.textContent = ''; shellOutText.classList.remove('sh-shimmer'); }
        if (shellOutLine) { shellOutLine.classList.remove('show', 'fail'); }
        if (shellLoader) { shellLoader.classList.remove('show'); }
    };

    // Swap the shell terminal in/out over the screenshot (#shellPanel overlay).
    const setShellPanel = (on) => {
        const p = document.getElementById('shellPanel');
        if (p) p.classList.toggle('is-active', on);
    };

    window.shellStart = (command, label) => {
        resetShellTerminal();
        if (!shellTerminalContainer) return;

        fadeScreenshot(true);
        setShellPanel(true);

        // Small coder spinner sits at the HEAD (right after `>`) the whole time the
        // command runs — never floats out to the wrapped line's end. Type the
        // command char-by-char; once typed, let it SHIMMER while we await the result.
        shellCmdFull = command || 'executing…';
        if (shellLoader) shellLoader.classList.add('show');
        if (shellCmdText) {
            shellCmdStream = streamChars(shellCmdText, shellCmdFull, () => {
                shellCmdText.classList.add('sh-shimmer');
            });
        }
    };

    window.shellResult = (status, output) => {
        resolveShellEls();
        if (!shellTerminalContainer) return;

        // Command finished running — force-complete its stream (show the full
        // command even if the result beat the typewriter), drop the shimmer + loader.
        if (shellCmdStream) { shellCmdStream.stop(); shellCmdStream = null; }
        if (shellCmdText) { shellCmdText.textContent = shellCmdFull; shellCmdText.classList.remove('sh-shimmer'); }
        if (shellLoader) shellLoader.classList.remove('show');

        const text = output
            ? (output.length > 120 ? output.substring(0, 120) + '…' : output)
            : (status === 'success' ? 'done' : 'failed');

        // Reveal the L-connected output line and type the result in — char-by-char
        // too, but WITHOUT shimmer (the shimmer is the command's running indicator).
        if (shellOutLine) {
            shellOutLine.classList.toggle('fail', status !== 'success');
            shellOutLine.classList.add('show');
        }
        if (shellOutText) {
            shellOutStream = streamChars(shellOutText, text);
        }
    };

    window.shellEnd = () => {
        resolveShellEls();
        setShellPanel(false);
        fadeScreenshot(false);
        setTimeout(resetShellTerminal, 700);
    };
})();
