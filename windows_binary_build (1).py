# Copyright 2026 Autouse AI — https://github.com/auto-use/Auto-Use
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# If you build on this project, please keep this header and credit
# Autouse AI (https://github.com/auto-use/Auto-Use) in forks and derivative works.
# A small attribution goes a long way toward a healthy open-source
# community — thank you for contributing.

"""
Auto Use - Production Build Script (Nuitka)
============================================
Compiles to native binary for code protection. This script also packs all
embedded resources (data files under Auto_Use/ and frontend/) into
_embedded_resources.py as the first step, so there's only one command to run.

Requirements:
    pip install nuitka ordered-set zstandard --break-system-packages

    Also requires a C compiler:
    - Windows: Install Visual Studio Build Tools (MSVC) or MinGW64
    - Recommended: VS Build Tools 2022 with "Desktop development with C++"

Run: python windows_binary_build.py

Output: AutoUse_Setup.exe in project root
"""

import os
import sys
import shutil
import base64
import subprocess
from pathlib import Path

# =============================================================================
# CONFIGURATION
# =============================================================================

APP_NAME = "Auto Use"
APP_VERSION = "1.4"
APP_PUBLISHER = "Ashish Yadav"
APP_EXE_NAME = "AutoUse"
ENTRY_POINT = "app.py"

# Code Signing Configuration
# For purchased certificate: Set path to your .pfx file and password
# For self-signed: Leave as None and the script will create one
CODE_SIGN_CERT_PATH = None  # e.g., r"C:\certs\AutoUse.pfx"
CODE_SIGN_PASSWORD = None   # Certificate password (if using .pfx)
CODE_SIGN_TIMESTAMP_URL = "http://timestamp.digicert.com"  # Timestamp server

# Inno Setup compiler path
INNO_SETUP_COMPILER = r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"

# Directories
ROOT_DIR = Path(__file__).parent.resolve()
BUILD_DIR = ROOT_DIR / "build"
DIST_DIR = ROOT_DIR / "dist"
OUTPUT_DIR = ROOT_DIR / "installer_output"

# Embedded-resource packer config (see pack_embedded_resources below)
RESOURCE_EXTENSIONS = {'.md', '.json', '.html', '.css', '.js', '.png', '.ico', '.jpg', '.jpeg', '.gif', '.mp4'}
RESOURCE_FOLDERS = ['Auto_Use', 'frontend']
RESOURCE_OUTPUT_FILE = '_embedded_resources.py'
API_KEY_FILE = os.path.join('Auto_Use', 'api_key', 'api_key.txt')

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def print_step(step_num, message):
    print(f"\n{'='*60}")
    print(f"  STEP {step_num}: {message}")
    print(f"{'='*60}\n")

def print_success(message):
    print(f"  [OK] {message}")

def print_error(message):
    print(f"  [ERROR] {message}")

def print_info(message):
    print(f"  [INFO] {message}")

def pre_flight_checks():
    """Production readiness checks - runs before packing resources.

    NOTE: the old ensure_frontend_platform_sync step is gone. frontend/script.js
    now picks the splash animation at runtime based on navigator.platform, and
    frontend/index.html no longer hardcodes a splash src — so rewriting those
    files at build time would actually corrupt the runtime-detection logic.
    Both windows_animation.html and mac_animation.html are packed as-is.
    """
    print_step("0a", "Pre-flight checks")

    issues_fixed = 0

    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, 'r') as f:
            lines = f.readlines()

        cleaned_lines = []
        changed = False
        for line in lines:
            if '=' in line:
                tag = line.split('=', 1)[0]
                cleaned = tag + '=\n'
                if cleaned != line.rstrip('\n') + '\n':
                    changed = True
                cleaned_lines.append(cleaned)
            else:
                cleaned_lines.append(line)

        if changed:
            with open(API_KEY_FILE, 'w') as f:
                f.writelines(cleaned_lines)
            print_success("Stripped API keys from api_key.txt")
            issues_fixed += 1
        else:
            print_success("api_key.txt - already clean")
    else:
        print_info("api_key.txt not found (will be created on first run)")

    print_info(f"Pre-flight: {issues_fixed} issue(s) fixed")


def pack_embedded_resources():
    """Walk RESOURCE_FOLDERS and base64-encode all matching files into _embedded_resources.py.

    The compiled binary imports this generated module and serves files from it via
    the patched `open()` and `serve_embedded_file` helpers in app.py.
    """
    print_step("0b", "Packing embedded resources")

    resources = {}
    total_size = 0

    for folder in RESOURCE_FOLDERS:
        folder_path = ROOT_DIR / folder
        if not folder_path.exists():
            print_info(f"Folder not found, skipping: {folder}")
            continue

        print_info(f"Scanning {folder}/")
        folder_count = 0

        for root, dirs, files in os.walk(folder_path):
            # Exclude runtime/temp folders and cache
            dirs[:] = [d for d in dirs if d not in ('__pycache__', '.git', 'venv', '.venv', 'scratchpad')]

            for filename in files:
                ext = Path(filename).suffix.lower()
                if ext not in RESOURCE_EXTENSIONS:
                    continue

                filepath = os.path.join(root, filename)
                # Normalize to forward-slash relative path as the dict key
                rel_path = os.path.relpath(filepath, ROOT_DIR).replace('\\', '/')

                try:
                    with open(filepath, 'rb') as f:
                        content = f.read()
                    resources[rel_path] = base64.b64encode(content).decode('ascii')
                    total_size += len(content)
                    folder_count += 1
                except Exception as e:
                    print_error(f"Failed to read {filepath}: {e}")

        print_info(f"  Found {folder_count} files in {folder}/")

    if not resources:
        print_error("No resources found to pack!")
        return False

    output_path = ROOT_DIR / RESOURCE_OUTPUT_FILE
    print_info(f"Writing {RESOURCE_OUTPUT_FILE}")

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('"""\n')
        f.write('Auto-generated embedded resources - DO NOT EDIT\n')
        f.write(f'Contains {len(resources)} files ({total_size / 1024:.1f} KB total)\n')
        f.write('"""\n\n')
        f.write('RESOURCES = {\n')

        for path, data in sorted(resources.items()):
            safe_path = path.replace('"', '\\"')
            f.write(f'    "{safe_path}":\n')
            f.write(f'        "{data}",\n')

        f.write('}\n')

    output_size = output_path.stat().st_size
    print_success(f"Packed {len(resources)} files ({total_size / 1024:.1f} KB source, {output_size / 1024:.1f} KB base64)")
    return True


def clean_build_directories():
    print_step(1, "Cleaning previous builds")
    
    for dir_path in [BUILD_DIR, DIST_DIR, OUTPUT_DIR]:
        if dir_path.exists():
            shutil.rmtree(dir_path)
            print_success(f"Removed {dir_path.name}/")
    
    # Clean Nuitka build artifacts
    nuitka_build = ROOT_DIR / f"{ENTRY_POINT.replace('.py', '.build')}"
    nuitka_dist = ROOT_DIR / f"{ENTRY_POINT.replace('.py', '.dist')}"
    nuitka_onefile = ROOT_DIR / f"{ENTRY_POINT.replace('.py', '.onefile-build')}"
    
    for nuitka_dir in [nuitka_build, nuitka_dist, nuitka_onefile]:
        if nuitka_dir.exists():
            shutil.rmtree(nuitka_dir)
            print_success(f"Removed {nuitka_dir.name}/")

def get_site_packages_path():
    """Find the site-packages directory that has our project's dependencies.
    
    Works whether running from a venv or global Python install.
    Tries importing a known package first, falls back to site module.
    """
    # Best method: derive from a known installed package
    for probe in ('flask', 'PIL', 'numpy', 'requests'):
        try:
            mod = __import__(probe)
            mod_file = getattr(mod, '__file__', None)
            if mod_file:
                candidate = Path(mod_file).parent.parent
                if candidate.name == 'site-packages':
                    return candidate
        except ImportError:
            continue

    # Fallback: use site module
    import site as _site
    for sp in _site.getsitepackages():
        p = Path(sp)
        if p.exists() and p.name == 'site-packages':
            return p

    return None


def copy_third_party_packages():
    """Copy all third-party packages from site-packages into the Nuitka dist.
    
    Nuitka compiles only our proprietary code (Auto_Use.windows_use) to native binary.
    Third-party packages are excluded from compilation (--nofollow-import-to)
    to prevent MSVC OOM and speed up builds. This step copies them as regular
    Python files so they're available at runtime.
    """
    print_step("4a", "Copying third-party packages to dist")

    site_packages = get_site_packages_path()
    if not site_packages or not site_packages.exists():
        print_error("Could not find site-packages directory!")
        print_error("Make sure you run the build from the environment with all packages installed.")
        return False

    dist_path = DIST_DIR / APP_EXE_NAME
    if not dist_path.exists():
        print_error(f"Dist directory not found: {dist_path}")
        return False

    print_info(f"Source: {site_packages}")
    print_info(f"Target: {dist_path}")

    SKIP_NAMES = {
        '__pycache__', 'pip', 'setuptools', 'pkg_resources', '_distutils_hack',
        'nuitka', 'ordered_set', 'zstandard', 'SCons',
        'Auto_Use',
        # Packages compiled by Nuitka -- don't overwrite with .py from site-packages
        'pywinauto', 'PIL', 'numpy', 'requests', 'dotenv', 'keyboard',
        'flask', 'comtypes', 'interception',
        # Transitive deps compiled via flask
        'werkzeug', 'jinja2', 'markupsafe', 'itsdangerous', 'click', 'blinker',
        # Transitive deps compiled via requests
        'urllib3', 'certifi', 'charset_normalizer', 'idna',
    }
    SKIP_SUFFIXES = ('.dist-info', '.egg-info', '.egg-link', '.pth', '.pyc')

    copied_dirs = 0
    copied_files = 0
    merged = 0

    for item in sorted(site_packages.iterdir()):
        name = item.name

        if any(name.endswith(s) for s in SKIP_SUFFIXES):
            continue
        if name in SKIP_NAMES:
            continue
        if name.startswith('~') or name.startswith('.'):
            continue

        dest = dist_path / name

        try:
            if item.is_dir():
                if dest.exists():
                    shutil.copytree(
                        item, dest,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
                        dirs_exist_ok=True,
                    )
                    merged += 1
                else:
                    shutil.copytree(
                        item, dest,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
                    )
                    copied_dirs += 1
            elif item.is_file():
                if not dest.exists():
                    shutil.copy2(item, dest)
                    copied_files += 1
        except Exception as e:
            print_error(f"Failed to copy {name}: {e}")

    print_success(f"Copied {copied_dirs} packages, {copied_files} files, merged {merged} existing")

    # Also copy .pth files that add directories to sys.path (e.g. pywin32.pth)
    pth_count = 0
    for pth in site_packages.glob('*.pth'):
        dest_pth = dist_path / pth.name
        if not dest_pth.exists():
            shutil.copy2(pth, dest_pth)
            pth_count += 1
    if pth_count:
        print_info(f"Copied {pth_count} .pth files for path resolution")

    return True


def convert_png_to_ico():
    print_step(2, "Preparing application icon")
    
    png_path = ROOT_DIR / "Auto_Use" / "logo" / "auto_use.png"
    ico_path = ROOT_DIR / "auto_use.ico"
    
    if not png_path.exists():
        print_error(f"Icon not found at {png_path}")
        return None
    
    try:
        from PIL import Image
        img = Image.open(png_path)
        # Order from largest to smallest - Windows uses first size as primary display
        sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
        icons = []
        
        for size in sizes:
            resized = img.resize(size, Image.Resampling.LANCZOS)
            if resized.mode != 'RGBA':
                resized = resized.convert('RGBA')
            icons.append(resized)
        
        # Save with largest icon as primary
        icons[0].save(ico_path, format='ICO', append_images=icons[1:])
        print_success(f"Created {ico_path.name}")
        return ico_path
        
    except Exception as e:
        print_error(f"Failed to convert icon: {e}")
        return None

def get_msvc_env():
    """Get environment variables from vcvarsall.bat for VS 2026 Community.

    Manually invoke vcvarsall.bat and capture the resulting environment so
    Nuitka/Scons can reliably locate cl.exe even when auto-detection misses it.
    """
    vcvarsall = r"C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvarsall.bat"
    
    if not Path(vcvarsall).exists():
        print_info("vcvarsall.bat not found, falling back to default environment")
        return None
    
    print_info(f"Setting up MSVC environment from VS 2026 Community...")
    
    try:
        # Run vcvarsall.bat and capture the resulting environment
        result = subprocess.run(
            f'"{vcvarsall}" x64 && set',
            capture_output=True, text=True, shell=True
        )
        
        if result.returncode != 0:
            print_error(f"vcvarsall.bat failed: {result.stderr}")
            return None
        
        # Parse environment variables from output
        env = {}
        for line in result.stdout.splitlines():
            if '=' in line:
                key, _, value = line.partition('=')
                env[key] = value
        
        print_success("MSVC environment configured")
        return env
        
    except Exception as e:
        print_error(f"Failed to set up MSVC environment: {e}")
        return None

def run_nuitka(icon_path):
    print_step(3, "Compiling to native binary with Nuitka")
    
    # Create dist directory
    DIST_DIR.mkdir(exist_ok=True)
    
    # Get MSVC environment (needed for VS 2026 Insiders which Nuitka can't auto-detect)
    msvc_env = get_msvc_env()
    
    cmd = [
        sys.executable, "-m", "nuitka",
        
        # Output settings
        "--standalone",                          # Create standalone distribution
        "--output-dir=" + str(DIST_DIR),         # Output directory
        "--output-filename=" + APP_EXE_NAME,     # Output executable name
        
        # Windows-specific
        "--windows-console-mode=disable",        # Hide console window (GUI app)
        "--windows-company-name=" + APP_PUBLISHER,
        "--windows-product-name=" + APP_NAME,
        "--windows-file-version=" + APP_VERSION,
        "--windows-product-version=" + APP_VERSION,
        
        # MSVC compiler (required for Python 3.13 - MinGW64 not supported)
        # Environment from vcvarsall.bat is passed to subprocess so Nuitka finds cl.exe
        "--msvc=latest",
        
        # Auto-accept download prompts (Dependency Walker, etc.)
        "--assume-yes-for-downloads",
        
        # Code protection / optimization
        "--lto=yes",                             # Link Time Optimization
        "--jobs=2",                              # Reduced from 4 to prevent MSVC OOM
        
        # Allow importing modules excluded via --nofollow-import-to at runtime.
        # We copy them as regular Python files in the post-build step.
        "--no-deployment-flag=excluded-module-usage",
    ]
    
    # Add icon if available
    if icon_path and icon_path.exists():
        cmd.append(f"--windows-icon-from-ico={icon_path}")
    
    # NOTE: frontend/ and Auto_Use/windows_use/sandbox/terminal/ are now embedded in _embedded_resources.py
    # and served via Flask routes - no need to include as data directories
    # This completely hides all folder structure from the user
    print_info("UI files embedded in binary (frontend/, terminal/)")
    
    # Create empty scratchpad folder structure post-build (done by app.py at runtime)
    
    # =========================================================================
    # COMPILE TO NATIVE BINARY
    # Everything here is compiled by Nuitka to C → native binary.
    # This was the working config before google-genai was added.
    # =========================================================================
    include_packages = [
        "pywinauto",
        "pywinauto.controls",
        "pywinauto.controls.uiawrapper",
        "PIL",
        "numpy",
        "requests",
        "dotenv",
        "keyboard",
        "flask",
        # Proprietary code (Auto_Use.windows_use)
        "Auto_Use",
        "Auto_Use.windows_use",
        "Auto_Use.windows_use.llm_provider.openrouter.view",
        "Auto_Use.windows_use.llm_provider.groq.view",
        "Auto_Use.windows_use.llm_provider.openai.view",
        "Auto_Use.windows_use.llm_provider.anthropic.view",
        "Auto_Use.windows_use.llm_provider.google.view",
    ]
    
    for package in include_packages:
        cmd.append(f"--include-package={package}")
    
    include_modules = [
        "_embedded_resources",
        "interception",
        "comtypes",
        "comtypes.client",
        "comtypes.stream",
        "win32api",
        "win32con",
        "win32gui",
        "pydoc",  # needed by scipy._lib._docscrape at runtime
    ]
    
    for module in include_modules:
        cmd.append(f"--include-module={module}")
    
    # =========================================================================
    # EXCLUDE FROM C COMPILATION (copied as Python by post-build step)
    #
    # Only packages that cause MSVC OOM or are open-source LLM SDKs.
    # When adding new large packages in the future (e.g. Azure SDK),
    # just add them here -- the post-build step copies them automatically.
    # =========================================================================
    nofollow_third_party = [
        # Entire google namespace (genai, cloud, auth, api_core, protobuf)
        # Must exclude as one unit to avoid namespace package conflicts
        "google",
        "aiohttp",
        "grpc",
        "protobuf",
        "scipy",
        "networkx",
        
        # LLM provider SDKs (open-source, no need to compile)
        "openai",
        "anthropic",
        "groq",
        
        # Never needed
        "tkinter",
        "matplotlib",
        "pytest",
        "setuptools",
    ]
    
    for pkg in nofollow_third_party:
        cmd.append(f"--nofollow-import-to={pkg}")
    
    # NOTE: Auto_Use is NOT copied as data - all resources are embedded in binary
    # This completely hides folder structure (no agent/, controller/, tree/ visible)
    
    # Add entry point
    cmd.append(str(ROOT_DIR / ENTRY_POINT))
    
    print_info("Running Nuitka (this may take 5-15 minutes on first build)...")
    print_info("Command: " + " ".join(cmd[:10]) + " ...")
    
    try:
        # Run Nuitka with real-time output, using MSVC environment if available
        process = subprocess.Popen(
            cmd,
            cwd=ROOT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=msvc_env  # Pass MSVC environment so Nuitka/Scons can find cl.exe
        )
        
        # Stream all output in real-time (no filtering, so we can see errors/prompts)
        for line in process.stdout:
            line = line.rstrip()
            if line:
                print(f"    {line}")
        
        process.wait()
        
        if process.returncode != 0:
            print_error(f"Nuitka failed with return code {process.returncode}")
            return False
        
        print_success("Nuitka compilation completed successfully")
        return True
        
    except FileNotFoundError:
        print_error("Nuitka not found! Install with: pip install nuitka ordered-set zstandard")
        return False
    except Exception as e:
        print_error(f"Nuitka failed: {e}")
        return False

def organize_dist():
    """Organize Nuitka output to match expected structure for Inno Setup"""
    print_step(4, "Organizing distribution files")
    
    # Nuitka creates: dist/app.dist/ containing the executable and dependencies
    nuitka_dist = DIST_DIR / f"{ENTRY_POINT.replace('.py', '.dist')}"
    final_dist = DIST_DIR / APP_EXE_NAME
    
    if not nuitka_dist.exists():
        # Check alternative location
        nuitka_dist = ROOT_DIR / f"{ENTRY_POINT.replace('.py', '.dist')}"
    
    if not nuitka_dist.exists():
        print_error(f"Nuitka output not found at expected locations")
        # List what's in dist
        if DIST_DIR.exists():
            print_info(f"Contents of {DIST_DIR}:")
            for item in DIST_DIR.iterdir():
                print_info(f"  - {item.name}")
        return False
    
    # Rename/move to expected location
    if final_dist.exists():
        shutil.rmtree(final_dist)
    
    shutil.move(str(nuitka_dist), str(final_dist))
    
    # Rename the executable if needed
    old_exe = final_dist / f"{ENTRY_POINT.replace('.py', '.exe')}"
    new_exe = final_dist / f"{APP_EXE_NAME}.exe"
    
    if old_exe.exists() and old_exe != new_exe:
        old_exe.rename(new_exe)
    
    print_success(f"Distribution organized at {final_dist}")
    
    # List final contents
    exe_count = len(list(final_dist.glob("*.exe")))
    dll_count = len(list(final_dist.glob("*.dll")))
    pyd_count = len(list(final_dist.glob("**/*.pyd")))
    print_info(f"Contents: {exe_count} exe, {dll_count} dll, {pyd_count} pyd files")
    
    return True

def create_self_signed_cert():
    """Create a self-signed certificate for code signing if none exists"""
    cert_path = ROOT_DIR / "certs"
    pfx_path = cert_path / "AutoUse.pfx"
    
    if pfx_path.exists():
        print_info("Using existing certificate")
        return pfx_path, "AutoUse123"  # Default password for self-signed
    
    print_info("Creating self-signed certificate...")
    cert_path.mkdir(exist_ok=True)
    
    # PowerShell command to create self-signed certificate
    ps_script = f'''
$cert = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject "CN={APP_PUBLISHER}, O={APP_NAME}" `
    -KeyAlgorithm RSA `
    -KeyLength 2048 `
    -HashAlgorithm SHA256 `
    -CertStoreLocation Cert:\\CurrentUser\\My `
    -NotAfter (Get-Date).AddYears(5)

$pwd = ConvertTo-SecureString -String "AutoUse123" -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath "{pfx_path}" -Password $pwd

# Also export to trusted root for local testing (optional)
$rootStore = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "CurrentUser")
$rootStore.Open("ReadWrite")
$rootStore.Add($cert)
$rootStore.Close()

Write-Host "Certificate created and exported to {pfx_path}"
Write-Host "Thumbprint: $($cert.Thumbprint)"
'''
    
    try:
        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0 and pfx_path.exists():
            print_success(f"Self-signed certificate created: {pfx_path}")
            return pfx_path, "AutoUse123"
        else:
            print_error(f"Failed to create certificate: {result.stderr}")
            return None, None
            
    except Exception as e:
        print_error(f"Certificate creation failed: {e}")
        return None, None

def sign_executable(exe_path, cert_path=None, password=None):
    """Sign an executable with a code signing certificate"""
    
    if not exe_path.exists():
        print_error(f"Executable not found: {exe_path}")
        return False
    
    # Determine certificate to use
    if cert_path and Path(cert_path).exists():
        pfx_path = Path(cert_path)
        pfx_password = password
    elif CODE_SIGN_CERT_PATH and Path(CODE_SIGN_CERT_PATH).exists():
        pfx_path = Path(CODE_SIGN_CERT_PATH)
        pfx_password = CODE_SIGN_PASSWORD
    else:
        # Create self-signed certificate
        pfx_path, pfx_password = create_self_signed_cert()
        if not pfx_path:
            print_error("No certificate available for signing")
            return False
    
    # Find signtool.exe
    signtool_paths = [
        r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe",
        r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.22000.0\x64\signtool.exe",
        r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.19041.0\x64\signtool.exe",
        r"C:\Program Files (x86)\Windows Kits\10\App Certification Kit\signtool.exe",
    ]
    
    signtool = None
    for path in signtool_paths:
        if Path(path).exists():
            signtool = path
            break
    
    if not signtool:
        # Try to find it via where command
        try:
            result = subprocess.run(["where", "signtool"], capture_output=True, text=True)
            if result.returncode == 0:
                signtool = result.stdout.strip().split('\n')[0]
        except:
            pass
    
    if not signtool:
        print_error("signtool.exe not found!")
        print_info("Install Windows SDK or Visual Studio with Windows SDK component")
        print_info("Download: https://developer.microsoft.com/en-us/windows/downloads/windows-sdk/")
        return False
    
    # Sign the executable
    cmd = [
        signtool,
        "sign",
        "/f", str(pfx_path),
        "/p", pfx_password,
        "/fd", "SHA256",
        "/tr", CODE_SIGN_TIMESTAMP_URL,
        "/td", "SHA256",
        "/d", APP_NAME,
        str(exe_path)
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            print_success(f"Signed: {exe_path.name}")
            return True
        else:
            print_error(f"Signing failed: {result.stderr}")
            return False
            
    except Exception as e:
        print_error(f"Signing error: {e}")
        return False

def sign_all_executables():
    """Sign all executables in the distribution"""
    print_step("4b", "Code signing executables")
    
    dist_path = DIST_DIR / APP_EXE_NAME
    
    if not dist_path.exists():
        print_error("Distribution directory not found")
        return False
    
    # Sign main executable
    main_exe = dist_path / f"{APP_EXE_NAME}.exe"
    if main_exe.exists():
        if not sign_executable(main_exe):
            print_info("Continuing without signature (UAC will show 'Unknown Publisher')")
    
    # Optionally sign DLLs (usually not necessary)
    # for dll in dist_path.glob("*.dll"):
    #     sign_executable(dll)
    
    return True

def create_inno_setup_script():
    print_step(5, "Creating Inno Setup script")
    
    dist_path = str(DIST_DIR / APP_EXE_NAME).replace("\\", "\\\\")
    output_path = str(OUTPUT_DIR).replace("\\", "\\\\")
    ico_path = str(ROOT_DIR / "auto_use.ico").replace("\\", "\\\\")
    license_path = str(ROOT_DIR / "LICENSE").replace("\\", "\\\\")
    interception_installer = str(ROOT_DIR / "Interception" / "command line installer" / "install-interception.exe").replace("\\", "\\\\")
    
    iss_content = f'''; Auto Use - Inno Setup Script (Nuitka Build)

#define MyAppName "{APP_NAME}"
#define MyAppVersion "{APP_VERSION}"
#define MyAppPublisher "{APP_PUBLISHER}"
#define MyAppExeName "{APP_EXE_NAME}.exe"

[Setup]
AppId={{{{8F3E9A5B-2C1D-4E7F-A8B9-1234567890AB}}}}
AppName={{#MyAppName}}
AppVersion={{#MyAppVersion}}
AppPublisher={{#MyAppPublisher}}
DefaultDirName={{localappdata}}\\{{#MyAppName}}
DefaultGroupName={{#MyAppName}}
AllowNoIcons=yes
OutputDir={output_path}
OutputBaseFilename=AutoUse_Setup
SetupIconFile={ico_path}
UninstallDisplayIcon={{app}}\\{{#MyAppExeName}}
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=admin
MinVersion=10.0
WizardStyle=modern
LicenseFile={license_path}
AlwaysRestart=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
Source: "{dist_path}\\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{interception_installer}"; DestDir: "{{app}}\\InterceptionDriver"; Flags: ignoreversion

[Icons]
Name: "{{group}}\\{{#MyAppName}}"; Filename: "{{app}}\\{{#MyAppExeName}}"; WorkingDir: "{{app}}"
Name: "{{group}}\\Uninstall {{#MyAppName}}"; Filename: "{{uninstallexe}}"
Name: "{{commondesktop}}\\{{#MyAppName}}"; Filename: "{{app}}\\{{#MyAppExeName}}"; WorkingDir: "{{app}}"; Tasks: desktopicon

[Run]
Filename: "{{app}}\\InterceptionDriver\\install-interception.exe"; Parameters: "/install"; Flags: runhidden waituntilterminated; StatusMsg: "Installing Interception driver..."

[UninstallRun]
Filename: "{{app}}\\InterceptionDriver\\install-interception.exe"; Parameters: "/uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "InterceptionUninstall"

[UninstallDelete]
Type: filesandordirs; Name: "{{app}}"

[Messages]
FinishedLabel=Setup has finished installing [name] on your computer.%n%nIMPORTANT: A RESTART IS REQUIRED for the Auto Use driver to work properly.%n%nClick Finish to restart your computer now.

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DeleteFile(ExpandConstant('{{sys}}\\drivers\\keyboard.sys'));
    DeleteFile(ExpandConstant('{{sys}}\\drivers\\mouse.sys'));
    
    if MsgBox('Uninstallation complete.' + #13#10 + #13#10 + 
              'A restart is REQUIRED to fully remove the Auto Use driver.' + #13#10 + #13#10 +
              'Restart now?', 
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      Exec('shutdown.exe', '/r /t 5 /c "Restarting to complete Auto Use uninstallation..."', '', SW_HIDE, ewNoWait, ResultCode);
    end;
  end;
end;

function NeedRestart(): Boolean;
begin
  Result := True;
end;
'''
    
    iss_path = ROOT_DIR / "installer.iss"
    with open(iss_path, 'w', encoding='utf-8') as f:
        f.write(iss_content)
    
    print_success(f"Created {iss_path.name}")
    return iss_path

def run_inno_setup(iss_path):
    print_step(6, "Compiling installer with Inno Setup")
    
    if not Path(INNO_SETUP_COMPILER).exists():
        print_error(f"Inno Setup not found at: {INNO_SETUP_COMPILER}")
        return False
    
    OUTPUT_DIR.mkdir(exist_ok=True)
    
    try:
        result = subprocess.run(
            [INNO_SETUP_COMPILER, str(iss_path)],
            check=True,
            capture_output=True,
            text=True
        )
        print_success("Inno Setup completed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"Inno Setup failed!")
        if e.stdout:
            print_error(f"STDOUT: {e.stdout}")
        if e.stderr:
            print_error(f"STDERR: {e.stderr}")
        return False

def move_final_installer():
    print_step(7, "Finalizing")
    
    installer_path = OUTPUT_DIR / "AutoUse_Setup.exe"
    final_path = ROOT_DIR / "AutoUse_Setup.exe"
    
    if installer_path.exists():
        if final_path.exists():
            final_path.unlink()
        shutil.move(str(installer_path), str(final_path))
        size_mb = final_path.stat().st_size / (1024 * 1024)
        print_success(f"Installer ready: {final_path.name} ({size_mb:.1f} MB)")
        return final_path
    else:
        print_error("Installer not found")
        return None

def cleanup():
    """Clean up build artifacts"""
    for dir_path in [BUILD_DIR, OUTPUT_DIR]:
        if dir_path.exists():
            shutil.rmtree(dir_path)
    
    # Clean Nuitka build artifacts (keep dist for debugging if needed)
    nuitka_build = ROOT_DIR / f"{ENTRY_POINT.replace('.py', '.build')}"
    nuitka_onefile = ROOT_DIR / f"{ENTRY_POINT.replace('.py', '.onefile-build')}"
    
    for nuitka_dir in [nuitka_build, nuitka_onefile]:
        if nuitka_dir.exists():
            shutil.rmtree(nuitka_dir)
    
    # Remove installer script
    iss_file = ROOT_DIR / "installer.iss"
    if iss_file.exists():
        iss_file.unlink()
    
    print_success("Cleanup complete")

# =============================================================================
# MAIN
# =============================================================================

def main():
    print("\n" + "="*60)
    print("     AUTO USE - PRODUCTION BUILD (NUITKA)")
    print("     Native Binary Compilation for Code Protection")
    print("="*60)

    # Step 0: Pre-flight checks + pack embedded resources
    pre_flight_checks()
    if not pack_embedded_resources():
        print_error("Resource packing failed!")
        sys.exit(1)

    # Step 1: Clean
    clean_build_directories()
    
    # Step 2: Icon
    icon_path = convert_png_to_ico()
    
    # Step 3: Nuitka compilation
    if not run_nuitka(icon_path):
        print_error("Nuitka compilation failed!")
        sys.exit(1)
    
    # Step 4: Organize distribution
    if not organize_dist():
        print_error("Failed to organize distribution!")
        sys.exit(1)
    
    # Step 4a: Copy third-party packages into dist (excluded from Nuitka compilation)
    if not copy_third_party_packages():
        print_error("Failed to copy third-party packages!")
        sys.exit(1)
    
    # Step 4b: Code sign executables
    sign_all_executables()
    
    # Step 5: Create Inno Setup script
    iss_path = create_inno_setup_script()
    
    # Step 6: Run Inno Setup
    if not run_inno_setup(iss_path):
        print_error("Inno Setup failed!")
        sys.exit(1)
    
    # Step 7: Move final installer
    final_installer = move_final_installer()
    
    # Step 7b: Sign the installer
    if final_installer:
        print_step("7b", "Signing installer")
        sign_executable(final_installer)
    
    if final_installer:
        cleanup()
        print("\n" + "="*60)
        print("     [OK] BUILD SUCCESSFUL!")
        print(f"     Output: {final_installer.name}")
        print("     ")
        print("     Code Protection: Native binary (C compiled)")
        print("     Reverse Engineering: Significantly harder")
        print(f"     Publisher: {APP_PUBLISHER}")
        print("     ")
        print("     NOTE: Self-signed cert shows your name in UAC but")
        print("     still shows a warning. For full trust with no warning,")
        print("     purchase a code signing certificate (~$200-500/year).")
        print("="*60 + "\n")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()