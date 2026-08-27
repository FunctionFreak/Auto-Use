# Third-Party Notices

Auto Use itself is released under the MIT License (see [LICENSE](LICENSE)).

The components listed below are **third-party works redistributed inside this
repository**. They are **not** covered by Auto Use's MIT license and are **not**
sublicensed by it. Each remains under its own terms, and the MIT grant in
[LICENSE](LICENSE) — including the permission to sell or sublicense — applies
only to Auto Use's own code, never to these components.

---

## 1. Interception — **not redistributed; fetched at setup time**

| | |
|---|---|
| **Author** | Francisco Lopes da Silva ("oblitum") |
| **Upstream** | https://github.com/oblitum/Interception |
| **How it reaches you** | **Not vendored in this repository.** `windows_setup.bat` downloads the author's own GitHub release directly to the user's machine. |
| **Pinned version** | `v1.0.1` — asset `Interception.zip` |
| **SHA-256** | `ad038963d6413055765128b0b931f6e765147c9916dba79e65d872b261f9af10` — verified before the installer is executed; setup aborts on mismatch |
| **License** | **Dual-licensed.** Non-commercial use: LGPL v3.0. Commercial use: requires a **separate paid commercial license** obtained from the author. Both texts ship inside the downloaded archive under `licenses/`. |

**This project does not redistribute Interception.** No Interception binary,
library, header, or installer is committed to this repository. The
[`Interception/`](Interception/) directory contains only
[`NOTICE.md`](Interception/NOTICE.md); its downloaded contents are gitignored.
Users obtain the driver from its author, over TLS, from the author's own release
URL, with the archive's checksum verified before anything is executed.

**Important — this is a carve-out from Auto Use's MIT license.**

Auto Use's MIT license permits commercial use, sublicensing, and sale *of Auto
Use's own source code*. It does **not** and **cannot** grant those rights over
Interception. If you ship, sell, or otherwise commercially distribute a product
that bundles or installs Interception — including any packaged Auto Use binary
that does so — **you must obtain a commercial license from the Interception
author yourself.** Auto Use's maintainers do not provide, resell, or extend one
to you.

**What it does.** Interception is a Windows kernel-mode keyboard/mouse filter
driver. Auto Use uses it for one purpose only: Windows' user-mode `SendInput`
API cannot deliver input to the UAC secure desktop, so responding to a UAC
elevation prompt requires kernel-mode input. See
[INTERCEPTION_DRIVER.md](INTERCEPTION_DRIVER.md) for the full technical writeup,
including the registry changes the driver makes and how to remove them.

Because it is a signed kernel input filter driver, some antivirus and EDR
products classify Interception under generic "HackTool" / "keyboard filter"
heuristics. The binaries here are the author's own unmodified release, signed
`CN=Francisco Lopes da Silva, C=BR`; verify the Authenticode signature before
trusting them.

---

## 2. WebDriverAgent — **not redistributed; cloned at setup time**

| | |
|---|---|
| **Authors** | Facebook, Inc. and the Appium project |
| **Upstream** | https://github.com/appium/WebDriverAgent |
| **How it reaches you** | **Not vendored in this repository.** [`ios_setup.sh`](ios_setup.sh) clones it from the Appium project directly onto the user's machine. |
| **Pinned version** | tag `v15.1.1` (`git clone --depth 1 --branch v15.1.1`) |
| **License** | BSD 3-Clause, with some files under Apache License 2.0 — the `LICENSE` file arrives with the clone |

Unlike Interception, **BSD 3-Clause expressly permits redistribution**, so
vendoring WebDriverAgent was never a licensing problem. It is fetched instead
simply so this repository contains no third-party source we did not write —
which also keeps Apple private-framework headers (`XCTest`, `UIKitCore`,
shipped by upstream WebDriverAgent) out of our tree entirely.

`Auto_Use/ios_connector/setup.py` rewrites the clone's
`WebDriverAgent.xcodeproj/project.pbxproj` locally to switch its targets to
automatic signing with the user's own Apple development team. That modification
happens on the user's machine and is never redistributed.

Neither Facebook, Meta, nor the Appium project endorses Auto Use; per the
BSD 3-Clause terms their names are not used to promote this project.

The clone also carries **CocoaAsyncSocket** (public domain / MIT), which
WebDriverAgent vendors upstream. It reaches users from Appium, not from us.

---

## 3. Python dependencies

Runtime dependencies in [`mac_requirements.txt`](mac_requirements.txt),
[`windows_requirements.txt`](windows_requirements.txt), and
[`ios_requirements.txt`](ios_requirements.txt) (optional iOS) are **not** vendored —
they are installed from PyPI at setup time and each remains under its own
license. Notable among them, `interception-python` is a Python binding that
requires the Interception driver covered in §1; the same dual-license terms
apply to the driver it talks to.

---

## Reporting a licensing concern

If you believe something here is misattributed, under-attributed, or
redistributed in a way its license does not permit, please open an issue at
https://gitlab.com/auto-use/auto-use/-/issues — we will correct it promptly.
