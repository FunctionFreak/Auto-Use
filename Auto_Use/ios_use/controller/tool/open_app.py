#!/usr/bin/env python3
"""
open_app.py - iPhone App Launcher Service
Handles app launching and navigation on iPhone via WebDriverAgent
"""

import json
import os
import subprocess
import requests
import logging

logger = logging.getLogger(__name__)


class AppLauncherService:
    """Service for launching and managing iPhone apps"""
    
    def __init__(self):
        self.wda_url = "http://localhost:8100"
        self.session_id = None
        self.apps_dict = {}
        
    def scan_apps(self):
        """Scan all installed apps using pymobiledevice3 (same CLI the WDA session uses)"""
        try:
            from Auto_Use.ios_connector.session import _pmd3_base
            base = _pmd3_base()
            if not base:
                logger.error("❌ Error: pymobiledevice3 not found.")
                logger.error("💡 Install it with: pip install pymobiledevice3")
                return False

            logger.info("📱 Scanning installed apps...")
            result = subprocess.run(
                base + ['apps', 'list'],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "NO_COLOR": "1"},
            )
            if result.returncode != 0:
                tail = (result.stderr or '').strip().splitlines()
                logger.error(f"❌ App scan failed: {tail[-1] if tail else 'pymobiledevice3 error'}")
                return False

            # Parse the apps
            self.parse_apps(result.stdout)
            logger.info(f"✅ Found {len(self.apps_dict)} apps")
            return True

        except FileNotFoundError:
            logger.error("❌ Error: pymobiledevice3 not found.")
            logger.error("💡 Install it with: pip install pymobiledevice3")
            return False
        except subprocess.TimeoutExpired:
            logger.error("❌ Timeout while scanning apps")
            return False
        except Exception as e:
            logger.error(f"❌ Error scanning apps: {e}")
            return False

    def parse_apps(self, apps_output):
        """Parse `pymobiledevice3 apps list` JSON into searchable dictionary"""
        self.apps_dict = {}

        for bundle_id, info in json.loads(apps_output).items():
            display_name = info.get('CFBundleDisplayName') or info.get('CFBundleName') or bundle_id
            version = info.get('CFBundleShortVersionString') or str(info.get('CFBundleVersion', ''))

            # Store both by bundle ID and display name (lowercase for searching)
            self.apps_dict[display_name.lower()] = {
                'bundle_id': bundle_id,
                'display_name': display_name,
                'version': version
            }
    
    def search_app(self, search_term):
        """Search for app by name or bundle ID (case-insensitive, partial matching)"""
        search_term = search_term.lower().strip().replace(' ', '')
        matches = []
        
        for app_name, app_info in self.apps_dict.items():
            # Search in display name (without spaces)
            app_name_no_space = app_name.replace(' ', '')
            
            # Search in bundle ID
            bundle_id_lower = app_info['bundle_id'].lower()
            
            # Match if search term is in app name or bundle ID
            if search_term in app_name_no_space or search_term in bundle_id_lower:
                matches.append(app_info)
        
        return matches
    
    def get_session(self):
        """Get or create WDA session"""
        try:
            # Try to create new session
            response = requests.post(
                f"{self.wda_url}/session",
                json={"capabilities": {"alwaysMatch": {}}},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                if 'sessionId' in data:
                    self.session_id = data['sessionId']
                elif 'value' in data and 'sessionId' in data['value']:
                    self.session_id = data['value']['sessionId']
                return self.session_id
        except Exception as e:
            logger.error(f"Session error: {e}")
        return None
    
    def launch_app(self, app_name):
        """Launch app by searching for it first, then calling WDA"""
        # Lazy-load the installed apps list on first launch
        if not self.apps_dict:
            self.scan_apps()

        # Search for the app
        matches = self.search_app(app_name)
        
        if not matches:
            logger.error(f"No apps found matching '{app_name}'")
            return False
        
        # Use first match
        selected_app = matches[0]
        bundle_id = selected_app['bundle_id']
        display_name = selected_app['display_name']
        
        # Now launch it using the internal method
        return self._do_launch(bundle_id, display_name)
    
    def _do_launch(self, bundle_id, app_name):
        """Launch app using WDA"""
        logger.info(f"🚀 Launching {app_name}...")
        
        # Get a fresh session for app launching
        session = self.get_session()
        if not session:
            logger.error("Failed to create WDA session")
            return False
        
        # Try multiple WDA endpoints for compatibility
        endpoints = [
            f"{self.wda_url}/wda/apps/launch",
            f"{self.wda_url}/session/{session}/wda/apps/launch",
            f"{self.wda_url}/wda/apps/activate",
            f"{self.wda_url}/session/{session}/wda/apps/activate",
        ]
        
        for endpoint in endpoints:
            try:
                response = requests.post(
                    endpoint,
                    json={"bundleId": bundle_id},
                    timeout=10
                )
                if response.status_code == 200:
                    logger.info(f"✅ Successfully launched {app_name}")
                    return True
            except Exception as e:
                continue
        
        logger.error(f"Failed to launch {app_name}")
        return False
    
    def go_home(self):
        """Go to home screen"""
        try:
            response = requests.post(f"{self.wda_url}/wda/homescreen", timeout=5)
            if response.status_code == 200:
                logger.info("Navigated to home screen")
                return True
                
            if self.session_id:
                response = requests.post(
                    f"{self.wda_url}/session/{self.session_id}/wda/pressButton",
                    json={"name": "home"},
                    timeout=5
                )
                if response.status_code == 200:
                    logger.info("Navigated to home screen")
                    return True
        except Exception as e:
            logger.error(f"Go home failed: {e}")
            
        return False
    
    def test_wda_connection(self):
        """Test connection to WDA"""
        try:
            response = requests.get(f"{self.wda_url}/status", timeout=5)
            if response.status_code == 200:
                return True
            return False
        except:
            return False


# Create global instance
app_launcher_service = AppLauncherService()