#!/usr/bin/env python3
"""
videoplayer.py - Standalone video player controller for iPhone
Controls fullscreen video playback with smart control reveal
"""

import requests
import time
import re
import xml.etree.ElementTree as ET

class VideoPlayerController:
    def __init__(self):
        self.wda_url = "http://localhost:8100"
        self.session_id = None
        
    def get_session(self):
        """Get or create WDA session"""
        try:
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
            pass
        return None
    
    def tap_location(self, x, y):
        """Tap at specific coordinates"""
        session = self.get_session()
        if not session:
            return False
            
        try:
            response = requests.post(
                f"{self.wda_url}/session/{session}/actions",
                json={
                    "actions": [{
                        "type": "pointer",
                        "id": "finger1",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {"type": "pointerMove", "duration": 0, "x": x, "y": y},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 10},
                            {"type": "pointerUp", "button": 0}
                        ]
                    }]
                },
                timeout=5
            )
            return response.status_code == 200
        except:
            return False
    
    def reveal_controls(self):
        """Tap left side of screen to reveal video controls"""
        # Tap at x=100, y=195 (middle of screen height)
        self.tap_location(100, 195)
        time.sleep(0.5)  # Wait for animation
    
    def scan_elements(self):
        """Scan current UI elements and return parsed data"""
        try:
            response = requests.get(f"{self.wda_url}/source")
            if response.status_code != 200:
                return None
                
            xml_string = response.json()['value']
            root = ET.fromstring(xml_string)
            
            elements = {
                'close_button': None,
                'play_button': None,
                'pause_button': None,
                'timestamp': None,
                'duration': None,
                'slider': None
            }
            
            # Find all elements
            for elem in root.iter():
                elem_type = elem.get('type', '')
                label = elem.get('label', '')
                value = elem.get('value', '')
                name = elem.get('name', '')
                
                # Close button
                if elem_type == 'XCUIElementTypeButton' and (
                    label.lower() == 'close' or 
                    name == 'Player_Close_Button'
                ):
                    elements['close_button'] = {
                        'x': float(elem.get('x', 0)) + float(elem.get('width', 0)) / 2,
                        'y': float(elem.get('y', 0)) + float(elem.get('height', 0)) / 2
                    }
                
                # Play button - BE SPECIFIC!
                elif elem_type == 'XCUIElementTypeButton' and (
                    name == 'Player_Play_Button' or 
                    (label.lower() == 'play' and 'airplay' not in label.lower())
                ):
                    elements['play_button'] = {
                        'x': float(elem.get('x', 0)) + float(elem.get('width', 0)) / 2,
                        'y': float(elem.get('y', 0)) + float(elem.get('height', 0)) / 2
                    }
                
                # Pause button - BE SPECIFIC!
                elif elem_type == 'XCUIElementTypeButton' and (
                    name == 'Player_Pause_Button' or 
                    label.lower() == 'pause'
                ):
                    elements['pause_button'] = {
                        'x': float(elem.get('x', 0)) + float(elem.get('width', 0)) / 2,
                        'y': float(elem.get('y', 0)) + float(elem.get('height', 0)) / 2
                    }
                
                # Slider (often contains time info)
                elif elem_type == 'XCUIElementTypeSlider' and name == 'Player_Progress_Bar':
                    elements['slider'] = {
                        'value': value,
                        'label': label,
                        'name': name
                    }
                
                # Timestamp - specifically looking for Player_Progress_Label
                elif elem_type == 'XCUIElementTypeStaticText' and name == 'Player_Progress_Label':
                    # Full timestamp like "00:01:44 / 00:53:50"
                    if '/' in label:
                        parts = label.split('/')
                        if len(parts) == 2:
                            time1 = re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)', parts[0])
                            time2 = re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)', parts[1])
                            if time1:
                                elements['timestamp'] = time1.group(1).strip()
                            if time2:
                                elements['duration'] = time2.group(1).strip()
            
            return elements
            
        except Exception as e:
            return None
    
    def ensure_controls_visible(self):
        """Ensure video controls are visible, reveal if needed"""
        elements = self.scan_elements()
        
        # Check if we have any control buttons or timestamp
        if not elements or (not elements['play_button'] and not elements['pause_button'] and not elements['timestamp']):
            self.reveal_controls()
            elements = self.scan_elements()
            
        return elements
    
    def close(self):
        """Close the video player"""
        elements = self.ensure_controls_visible()
        if not elements or not elements['close_button']:
            print("❌ Close button not found")
            return False
            
        if self.tap_location(elements['close_button']['x'], elements['close_button']['y']):
            print("✅ Video player closed")
            return True
        else:
            print("❌ Failed to close player")
            return False
    
    def check_streaming(self):
        """Check if video is currently streaming/playing"""
        # Get initial state
        elements1 = self.ensure_controls_visible()
        if not elements1:
            print("❌ Cannot read controls")
            return
            
        # If we can't find timestamp, try to use play/pause button state
        if not elements1['timestamp']:
            if elements1['pause_button']:
                print("✅ VIDEO IS STREAMING")
            elif elements1['play_button']:
                print("❌ VIDEO IS NOT STREAMING")
            else:
                print("❓ Cannot determine state")
            return
            
        timestamp1 = elements1['timestamp']
        
        # Wait 4 seconds
        time.sleep(4)
        
        # Check again
        elements2 = self.ensure_controls_visible()
        if not elements2 or not elements2['timestamp']:
            if elements2 and elements2['pause_button']:
                print("✅ VIDEO IS STREAMING")
            else:
                print("❌ Cannot determine state")
            return
            
        timestamp2 = elements2['timestamp']
        
        # Determine streaming status
        if timestamp1 != timestamp2:
            print("✅ VIDEO IS STREAMING")
        else:
            print("❌ VIDEO IS NOT STREAMING")
    
    def pause(self):
        """Pause the video"""
        elements = self.ensure_controls_visible()
        if not elements:
            print("❌ Cannot read controls")
            return False
            
        if not elements['pause_button']:
            if elements['play_button']:
                print("ℹ️  Video is already paused")
            else:
                print("❌ Pause button not found")
            return False
        
        # Click pause
        if not self.tap_location(elements['pause_button']['x'], elements['pause_button']['y']):
            print("❌ Failed to pause")
            return False
            
        # Verify pause worked
        time.sleep(0.5)
        elements_after = self.ensure_controls_visible()
        
        if elements_after and elements_after['play_button']:
            print("✅ Video paused")
            return True
        else:
            print("❌ Pause failed")
            return False
    
    def play(self):
        """Play the video"""
        elements = self.ensure_controls_visible()
        if not elements:
            print("❌ Cannot read controls")
            return False
            
        if not elements['play_button']:
            if elements['pause_button']:
                print("ℹ️  Video is already playing")
            else:
                print("❌ Play button not found")
            return False
        
        # Click play
        if not self.tap_location(elements['play_button']['x'], elements['play_button']['y']):
            print("❌ Failed to play")
            return False
            
        # Verify play worked
        time.sleep(0.5)
        elements_after = self.ensure_controls_visible()
        
        if elements_after and elements_after['pause_button']:
            print("✅ Video playing")
            return True
        else:
            print("❌ Play failed")
            return False
    
    def show_menu(self):
        """Display interactive menu"""
        print("\n" + "="*40)
        print("VIDEO PLAYER CONTROLLER")
        print("="*40)
        print("1. Close video player")
        print("2. Check if streaming")
        print("3. Pause video")
        print("4. Play video")
        print("5. Exit")
        print("="*40)
        
        choice = input("\nSelect option (1-5): ").strip()
        
        if choice == '1':
            self.close()
        elif choice == '2':
            self.check_streaming()
        elif choice == '3':
            self.pause()
        elif choice == '4':
            self.play()
        elif choice == '5':
            return False
        else:
            print("❌ Invalid option")
            
        return True  # Continue running

def main():
    """Main entry point"""
    controller = VideoPlayerController()
    
    # Keep showing menu until user exits
    running = True
    while running:
        running = controller.show_menu()

if __name__ == "__main__":
    main()