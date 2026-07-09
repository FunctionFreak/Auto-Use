#!/usr/bin/env python3
"""
Enhanced app.py - Scan and Launch iPhone Applications
Scans installed apps and launches the specified app via WDA
"""

import subprocess
import requests
import sys
import re

class AppScanner:
    def __init__(self):
        self.wda_url = "http://localhost:8100"
        self.session_id = None
        self.apps_dict = {}
        
    def scan_apps(self):
        """Scan all installed apps using ideviceinstaller"""
        try:
            print("📱 Scanning installed apps...")
            result = subprocess.run(
                ['ideviceinstaller', '-l', '-o', 'list_all'], 
                capture_output=True, 
                text=True,
                timeout=30
            )
            
            # Write to file
            with open('apps.txt', 'w') as f:
                f.write(result.stdout)
            
            # Parse the apps
            self.parse_apps(result.stdout)
            print(f"✅ Found {len(self.apps_dict)} apps\n")
            return True
            
        except FileNotFoundError:
            print("❌ Error: ideviceinstaller not found.")
            print("💡 Install it with: brew install ideviceinstaller")
            return False
        except subprocess.TimeoutExpired:
            print("❌ Timeout while scanning apps")
            return False
        except Exception as e:
            print(f"❌ Error scanning apps: {e}")
            return False
    
    def parse_apps(self, apps_output):
        """Parse apps.txt format into searchable dictionary"""
        self.apps_dict = {}
        
        for line in apps_output.strip().split('\n'):
            # Skip header line
            if line.startswith('CFBundleIdentifier'):
                continue
            
            # Parse format: bundle_id, "version", "display_name"
            parts = line.split(', ')
            if len(parts) >= 3:
                bundle_id = parts[0].strip()
                display_name = parts[2].strip().strip('"')
                
                # Store both by bundle ID and display name (lowercase for searching)
                self.apps_dict[display_name.lower()] = {
                    'bundle_id': bundle_id,
                    'display_name': display_name,
                    'version': parts[1].strip().strip('"')
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
            print(f"⚠️  Session error: {e}")
        return None
    
    def launch_app(self, bundle_id, app_name):
        """Launch app using WDA"""
        print(f"🚀 Launching {app_name}...")
        
        # Ensure session exists
        if not self.session_id:
            session = self.get_session()
            if not session:
                print("❌ Failed to create WDA session")
                return False
        
        # Try multiple WDA endpoints for compatibility
        endpoints = [
            f"{self.wda_url}/wda/apps/launch",
            f"{self.wda_url}/session/{self.session_id}/wda/apps/launch",
            f"{self.wda_url}/wda/apps/activate",
            f"{self.wda_url}/session/{self.session_id}/wda/apps/activate",
        ]
        
        for endpoint in endpoints:
            try:
                response = requests.post(
                    endpoint,
                    json={"bundleId": bundle_id},
                    timeout=10
                )
                if response.status_code == 200:
                    print(f"✅ Successfully launched {app_name}")
                    return True
            except Exception as e:
                continue
        
        print(f"❌ Failed to launch {app_name}")
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

def main():
    print("=" * 50)
    print("📱 iPhone App Scanner & Launcher")
    print("=" * 50)
    print()
    
    scanner = AppScanner()
    
    # Test WDA connection first
    if not scanner.test_wda_connection():
        print("❌ Cannot connect to WDA at http://localhost:8100")
        print("💡 Make sure WebDriverAgent is running on your iPhone")
        return
    
    print("✅ Connected to WDA\n")
    
    # Scan apps
    if not scanner.scan_apps():
        return
    
    # Get app name from command line or prompt
    if len(sys.argv) > 1:
        search_term = ' '.join(sys.argv[1:])
    else:
        search_term = input("🔍 Enter app name to launch (e.g., 'sky news', 'sky go'): ").strip()
    
    if not search_term:
        print("❌ No app name provided")
        return
    
    # Search for the app
    print(f"\n🔎 Searching for '{search_term}'...")
    matches = scanner.search_app(search_term)
    
    if not matches:
        print(f"❌ No apps found matching '{search_term}'")
        print("\n💡 Try searching with different keywords")
        return
    
    # If multiple matches, show them and let user choose
    if len(matches) > 1:
        print(f"\n✅ Found {len(matches)} matching apps:")
        for i, app in enumerate(matches, 1):
            print(f"  [{i}] {app['display_name']}")
        
        choice = input(f"\nSelect app number (1-{len(matches)}): ").strip()
        try:
            choice_idx = int(choice) - 1
            if 0 <= choice_idx < len(matches):
                selected_app = matches[choice_idx]
            else:
                print("❌ Invalid selection")
                return
        except ValueError:
            print("❌ Invalid input")
            return
    else:
        selected_app = matches[0]
        print(f"✅ Found: {selected_app['display_name']}")
    
    # Launch the app
    print()
    scanner.launch_app(selected_app['bundle_id'], selected_app['display_name'])

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Cancelled by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")