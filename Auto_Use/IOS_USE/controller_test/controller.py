#!/usr/bin/env python3
"""
controller.py - Action controller for iPhone automation
Reads element coordinates from element.txt and performs actions via WDA
"""

import requests
import re
import time
import os

class Controller:
    def __init__(self):
        self.wda_url = "http://localhost:8100"
        self.elements = {}
        self.session_id = None
        # Get the directory where this script is located
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.element_file = os.path.join(self.script_dir, 'element.txt')
        
    def load_elements(self):
        """Load element data from element.txt"""
        self.elements = {}
        try:
            print(f"📂 Loading elements from: {self.element_file}")
            with open(self.element_file, 'r', encoding='utf-8') as f:
                for line in f:
                    # Parse lines like: [8]<type="button", label="...", value="", x="115.0", y="488.0", w="160.0", h="40.0" />
                    # Extract element number and coordinates
                    coord_match = re.search(r'\[(\d+)\].*x="([\d.]+)".*y="([\d.]+)".*w="([\d.]+)".*h="([\d.]+)"', line)
                    if coord_match:
                        elem_num = int(coord_match.group(1))
                        x = float(coord_match.group(2))
                        y = float(coord_match.group(3))
                        width = float(coord_match.group(4))
                        height = float(coord_match.group(5))
                        
                        # Extract value attribute (text content of the element)
                        value_match = re.search(r'value="([^"]*)"', line)
                        value = value_match.group(1) if value_match else ""
                        
                        # Calculate center point for clicking
                        center_x = int(x + width / 2)
                        center_y = int(y + height / 2)
                        
                        self.elements[elem_num] = {
                            'x': int(x),
                            'y': int(y),
                            'width': int(width),
                            'height': int(height),
                            'center_x': center_x,
                            'center_y': center_y,
                            'value': value
                        }
            print(f"✅ Loaded {len(self.elements)} elements")
            return True
        except FileNotFoundError:
            print("❌ element.txt not found. Run scan first!")
            return False
        except Exception as e:
            print(f"❌ Error loading elements: {e}")
            return False
    
    def get_session(self):
        """Get or create WDA session"""
        try:
            # Create new session
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
    
    def click(self, element_number):
        """Click on element by number"""
        # Load latest element data
        if not self.load_elements():
            return False
            
        if element_number not in self.elements:
            print(f"❌ Element #{element_number} not in loaded elements: {list(self.elements.keys())}")
            raise ValueError(f"Element #{element_number} not found")
        
        elem = self.elements[element_number]
        print(f"🎯 Clicking at ({elem['center_x']}, {elem['center_y']})")
        
        # Get or create session
        session = self.get_session()
        if not session:
            raise Exception("Failed to create WDA session")
        
        # Use W3C Actions API for tapping
        try:
            response = requests.post(
                f"{self.wda_url}/session/{session}/actions",
                json={
                    "actions": [{
                        "type": "pointer",
                        "id": "finger1",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {"type": "pointerMove", "duration": 0, "x": elem['center_x'], "y": elem['center_y']},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 10},
                            {"type": "pointerUp", "button": 0}
                        ]
                    }]
                },
                timeout=5
            )
            
            if response.status_code == 200:
                return True
            else:
                raise Exception(f"WDA tap failed: {response.text}")
                
        except Exception as e:
            raise Exception(f"Click failed: {e}")
    
    def type_text(self, element_number, text):
        """Type text into element by number
        
        If element already has text, it will be deleted first before typing new text.
        Sends delete/backspace key for each existing character including spaces.
        """
        # Load latest elements to get current value
        if not self.load_elements():
            raise Exception("Failed to load elements")
        
        if element_number not in self.elements:
            raise ValueError(f"Element #{element_number} not found")
        
        # Get current value of the element
        current_value = self.elements[element_number].get('value', '')
        
        # First click on the element to focus it
        self.click(element_number)
        time.sleep(0.1)  # Small delay for focus
        
        # Get session
        session = self.get_session()
        if not session:
            raise Exception("Failed to create WDA session")
        
        # If element has existing text (not blank), delete it first
        if current_value and current_value.strip():
            # Send delete/backspace key for each character including spaces
            delete_keys = '\u0008' * len(current_value)
            
            try:
                requests.post(
                    f"{self.wda_url}/session/{session}/wda/keys",
                    json={'value': [delete_keys]},
                    timeout=10
                )
            except Exception as e:
                print(f"⚠️ Clear text error: {e}")
            
            time.sleep(0.1)  # Small delay after deletion
        
        # Now type the new text
        try:
            response = requests.post(
                f"{self.wda_url}/session/{session}/wda/keys",
                json={'value': [text]},
                timeout=10
            )
            
            if response.status_code == 200:
                return True
            else:
                raise Exception(f"WDA type failed: {response.text}")
                
        except Exception as e:
            raise Exception(f"Type failed: {e}")
    
    def clear_text(self):
        """Clear text in focused element"""
        session = self.get_session()
        if not session:
            return
            
        try:
            # Send clear command
            requests.post(
                f"{self.wda_url}/session/{session}/clear",
                timeout=5
            )
        except:
            pass  # Optional clear
    
    def scroll(self, element_number, direction):
        """Scroll/swipe within element bounds in specified direction
        
        Args:
            element_number: The element to scroll within
            direction: 'up', 'down', 'left', or 'right'
        """
        # Load latest element data
        if not self.load_elements():
            return False
            
        if element_number not in self.elements:
            print(f"❌ Element #{element_number} not in loaded elements: {list(self.elements.keys())}")
            raise ValueError(f"Element #{element_number} not found")
        
        elem = self.elements[element_number]
        direction = direction.lower()
        
        # Dynamic approach: Start from edge in SAME direction, swipe in that direction
        # This avoids center overlays and ensures the swipe starts from the correct edge
        
        # Margins and swipe distances
        margin = 20  # Safe margin from edge
        vertical_swipe_distance = 200  # More power for up/down swipes
        horizontal_swipe_distance = 200  # More power for left/right swipes
        
        # Calculate center for positioning
        center_x = elem['x'] + elem['width'] // 2
        center_y = elem['y'] + elem['height'] // 2
        
        # Calculate start and end points based on direction
        # Start from edge in the SAME direction and swipe in that direction
        if direction == 'up':
            # Start from top area, swipe upward (toward top edge)
            start_x = center_x
            start_y = elem['y'] + margin + vertical_swipe_distance
            end_x = center_x
            end_y = elem['y'] + margin
        elif direction == 'down':
            # Start from bottom area, swipe downward (toward bottom edge)
            start_x = center_x
            start_y = elem['y'] + elem['height'] - margin - vertical_swipe_distance
            end_x = center_x
            end_y = elem['y'] + elem['height'] - margin
        elif direction == 'left':
            # Start from left area, swipe leftward (toward left edge) - MORE POWER
            start_x = elem['x'] + margin + horizontal_swipe_distance
            start_y = center_y
            end_x = elem['x'] + margin
            end_y = center_y
        elif direction == 'right':
            # Start from right area, swipe rightward (toward right edge) - MORE POWER
            start_x = elem['x'] + elem['width'] - margin - horizontal_swipe_distance
            start_y = center_y
            end_x = elem['x'] + elem['width'] - margin
            end_y = center_y
        else:
            raise ValueError(f"Invalid direction: {direction}. Use: up, down, left, or right")
        
        # Get or create session
        session = self.get_session()
        if not session:
            raise Exception("Failed to create WDA session")
        
        # Use W3C Actions API for swipe gesture
        try:
            response = requests.post(
                f"{self.wda_url}/session/{session}/actions",
                json={
                    "actions": [{
                        "type": "pointer",
                        "id": "finger1",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {"type": "pointerMove", "duration": 0, "x": start_x, "y": start_y},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 100},
                            {"type": "pointerMove", "duration": 500, "x": end_x, "y": end_y},
                            {"type": "pointerUp", "button": 0}
                        ]
                    }]
                },
                timeout=5
            )
            
            if response.status_code == 200:
                return True
            else:
                raise Exception(f"WDA scroll failed: {response.text}")
                
        except Exception as e:
            raise Exception(f"Scroll failed: {e}")

# Create global instance for easy import
controller = Controller()

# Convenience functions for direct import
def click(element_number):
    """Click on element by number"""
    return controller.click(element_number)

def type_text(element_number, text):
    """Type text into element by number"""
    return controller.type_text(element_number, text)

def scroll(element_number, direction):
    """Scroll element in specified direction"""
    return controller.scroll(element_number, direction)

# Test functionality if run directly
if __name__ == "__main__":
    print("🎮 Controller Test Mode")
    print("=" * 30)
    
    ctrl = Controller()
    
    if ctrl.load_elements():
        print(f"✅ Loaded {len(ctrl.elements)} elements")
        print("\nElements:")
        for num, elem in sorted(ctrl.elements.items())[:5]:  # Show first 5
            print(f"  [{num}] at ({elem['center_x']}, {elem['center_y']})")
    else:
        print("❌ Could not load elements")