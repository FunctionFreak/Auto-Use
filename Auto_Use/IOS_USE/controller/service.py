import requests
import time
import logging
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

class ControllerService:
    """Service for executing iPhone actions via WebDriverAgent"""
    
    def __init__(self):
        self.wda_url = "http://localhost:8100"
        self.elements = {}
        self.session_id = None
    
    def update_elements(self, elements_mapping):
        """Update element mapping from scanner"""
        self.elements = elements_mapping
        logger.debug(f"Updated elements: {len(self.elements)} elements")
    
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
            logger.error(f"Session error: {e}")
        return None
    
    def click(self, element_number):
        """Click on element by number"""
        elem_num = int(element_number)
        
        if str(elem_num) not in self.elements:
            logger.error(f"Element #{elem_num} not in loaded elements: {list(self.elements.keys())}")
            raise ValueError(f"Element #{elem_num} not found")
        
        elem_data = self.elements[str(elem_num)]
        bounds = elem_data['bounds']
        
        # Calculate center point
        center_x = bounds['x'] + bounds['width'] // 2
        center_y = bounds['y'] + bounds['height'] // 2
        
        logger.debug(f"Clicking at ({center_x}, {center_y})")
        
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
                            {"type": "pointerMove", "duration": 0, "x": center_x, "y": center_y},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 100},
                            {"type": "pointerUp", "button": 0}
                        ]
                    }]
                },
                timeout=5
            )
            
            if response.status_code == 200:
                logger.info(f"✓ Click successful on element {elem_num} at ({center_x}, {center_y})")
                return True
            else:
                logger.error(f"✗ WDA tap failed: {response.status_code} - {response.text}")
                raise Exception(f"WDA tap failed: {response.text}")
                
        except Exception as e:
            logger.error(f"✗ Click exception: {str(e)}")
            raise Exception(f"Click failed: {e}")
    
    def type_text(self, element_number, text):
        """Type text into element by number
        
        If element already has text, it will be deleted first before typing new text.
        Sends delete/backspace key for each existing character including spaces.
        """
        elem_num = int(element_number)
        
        if str(elem_num) not in self.elements:
            raise ValueError(f"Element #{elem_num} not found")
        
        # Get current value of the element
        current_value = self.elements[str(elem_num)].get('value', '')
        
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
                logger.debug(f"Cleared {len(current_value)} characters from element")
            except Exception as e:
                logger.warning(f"Clear text error: {e}")
            
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
    
    def scroll(self, element_number, direction):
        """Scroll/swipe within element bounds in specified direction
        
        Args:
            element_number: The element to scroll within
            direction: 'up', 'down', 'left', or 'right'
        """
        elem_num = int(element_number)
        
        if str(elem_num) not in self.elements:
            logger.error(f"Element #{elem_num} not in loaded elements: {list(self.elements.keys())}")
            raise ValueError(f"Element #{elem_num} not found")
        
        elem_data = self.elements[str(elem_num)]
        bounds = elem_data['bounds']
        direction = direction.lower()
        
        # Dynamic approach: Start from edge in SAME direction, swipe in that direction
        # This avoids center overlays and ensures the swipe starts from the correct edge
        
        # Margins and swipe distances
        margin = 20  # Safe margin from edge
        vertical_swipe_distance = 200  # More power for up/down swipes
        horizontal_swipe_distance = 200  # More power for left/right swipes
        
        # Calculate center for positioning
        center_x = bounds['x'] + bounds['width'] // 2
        center_y = bounds['y'] + bounds['height'] // 2
        
        # Calculate start and end points based on direction
        # Start from edge in the SAME direction and swipe in that direction
        if direction == 'up':
            # Start from top area, swipe upward (toward top edge)
            start_x = center_x
            start_y = bounds['y'] + margin + vertical_swipe_distance
            end_x = center_x
            end_y = bounds['y'] + margin
        elif direction == 'down':
            # Start from bottom area, swipe downward (toward bottom edge)
            start_x = center_x
            start_y = bounds['y'] + bounds['height'] - margin - vertical_swipe_distance
            end_x = center_x
            end_y = bounds['y'] + bounds['height'] - margin
        elif direction == 'left':
            # Start from left area, swipe leftward (toward left edge) - MORE POWER
            start_x = bounds['x'] + margin + horizontal_swipe_distance
            start_y = center_y
            end_x = bounds['x'] + margin
            end_y = center_y
        elif direction == 'right':
            # Start from right area, swipe rightward (toward right edge) - MORE POWER
            start_x = bounds['x'] + bounds['width'] - margin - horizontal_swipe_distance
            start_y = center_y
            end_x = bounds['x'] + bounds['width'] - margin
            end_y = center_y
        else:
            raise ValueError(f"Invalid direction: {direction}. Use: up, down, left, or right")
        
        logger.debug(f"Scrolling {direction} from ({start_x}, {start_y}) to ({end_x}, {end_y})")
        
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
            
    # video player related functions
    def video_close(self):
        """Close the video player"""
        try:
            # Ensure controls are visible
            elements = self._video_ensure_controls_visible()
            if not elements or not elements['close_button']:
                logger.error("❌ Close button not found")
                return False
            
            # Tap close button
            session = self.get_session()
            if not session:
                logger.error("❌ Failed to get session")
                return False
                
            close_btn = elements['close_button']
            logger.info(f"🎬 Tapping close button at ({close_btn['x']}, {close_btn['y']})")
            
            response = requests.post(
                f"{self.wda_url}/session/{session}/actions",
                json={
                    "actions": [{
                        "type": "pointer",
                        "id": "finger1",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {"type": "pointerMove", "duration": 0, "x": close_btn['x'], "y": close_btn['y']},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 10},
                            {"type": "pointerUp", "button": 0}
                        ]
                    }]
                },
                timeout=5
            )
            
            if response.status_code == 200:
                logger.info("✅ Video player closed")
                return True
            else:
                logger.error(f"❌ Failed to close: {response.status_code}")
                return False
            
        except Exception as e:
            logger.error(f"❌ Video close error: {e}")
            return False
    
    def video_check_streaming(self):
        """Check if video is currently streaming/playing"""
        try:
            # Get initial state
            elements1 = self._video_ensure_controls_visible()
            if not elements1:
                return False
            
            # If no timestamp, check button state
            if not elements1['timestamp']:
                return elements1['pause_button'] is not None
            
            timestamp1 = elements1['timestamp']
            
            # Wait and check again
            time.sleep(4)
            
            elements2 = self._video_ensure_controls_visible()
            if not elements2 or not elements2['timestamp']:
                return elements2 and elements2['pause_button'] is not None
            
            timestamp2 = elements2['timestamp']
            
            # Video is streaming if timestamp changed
            return timestamp1 != timestamp2
            
        except Exception as e:
            logger.error(f"Video streaming check error: {e}")
            return False
    
    def video_pause(self):
        """Pause the video"""
        try:
            elements = self._video_ensure_controls_visible()
            if not elements:
                return False
            
            if not elements['pause_button']:
                # Already paused
                return True if elements['play_button'] else False
            
            # Click pause button
            session = self.get_session()
            if not session:
                return False
                
            pause_btn = elements['pause_button']
            response = requests.post(
                f"{self.wda_url}/session/{session}/actions",
                json={
                    "actions": [{
                        "type": "pointer",
                        "id": "finger1",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {"type": "pointerMove", "duration": 0, "x": pause_btn['x'], "y": pause_btn['y']},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 10},
                            {"type": "pointerUp", "button": 0}
                        ]
                    }]
                },
                timeout=5
            )
            
            if response.status_code != 200:
                return False
                
            # Verify pause worked
            time.sleep(0.5)
            elements_after = self._video_ensure_controls_visible()
            
            return elements_after and elements_after['play_button'] is not None
            
        except Exception as e:
            logger.error(f"Video pause error: {e}")
            return False
    
    def video_play(self):
        """Play the video"""
        try:
            elements = self._video_ensure_controls_visible()
            if not elements:
                return False
            
            if not elements['play_button']:
                # Already playing
                return True if elements['pause_button'] else False
            
            # Click play button
            session = self.get_session()
            if not session:
                return False
                
            play_btn = elements['play_button']
            response = requests.post(
                f"{self.wda_url}/session/{session}/actions",
                json={
                    "actions": [{
                        "type": "pointer",
                        "id": "finger1",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {"type": "pointerMove", "duration": 0, "x": play_btn['x'], "y": play_btn['y']},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 10},
                            {"type": "pointerUp", "button": 0}
                        ]
                    }]
                },
                timeout=5
            )
            
            if response.status_code != 200:
                return False
                
            # Verify play worked
            time.sleep(0.5)
            elements_after = self._video_ensure_controls_visible()
            
            return elements_after and elements_after['pause_button'] is not None
            
        except Exception as e:
            logger.error(f"Video play error: {e}")
            return False
    
    def _video_reveal_controls(self):
        """Tap to reveal video controls"""
        session = self.get_session()
        if not session:
            return
            
        try:
            # Tap left side of screen
            requests.post(
                f"{self.wda_url}/session/{session}/actions",
                json={
                    "actions": [{
                        "type": "pointer",
                        "id": "finger1",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {"type": "pointerMove", "duration": 0, "x": 100, "y": 195},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 10},
                            {"type": "pointerUp", "button": 0}
                        ]
                    }]
                },
                timeout=5
            )
        except:
            pass
    
    def _video_scan_elements(self):
        """Scan video player elements"""
        try:
            response = requests.get(f"{self.wda_url}/source")
            if response.status_code != 200:
                logger.error(f"❌ Failed to get source: {response.status_code}")
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
            
            buttons_found = []
            top_left_buttons = []  # For fallback close button detection
            
            # Find all elements
            for elem in root.iter():
                elem_type = elem.get('type', '')
                label = elem.get('label', '')
                value = elem.get('value', '')
                name = elem.get('name', '')
                
                # Log buttons for debugging
                if elem_type == 'XCUIElementTypeButton':
                    x = float(elem.get('x', 0))
                    y = float(elem.get('y', 0))
                    buttons_found.append(f"Button: name='{name}', label='{label}', pos=({x},{y})")
                    
                    # Track top-left buttons as potential close buttons
                    if x < 100 and y < 100:  # Top-left corner
                        top_left_buttons.append({
                            'x': x + float(elem.get('width', 0)) / 2,
                            'y': y + float(elem.get('height', 0)) / 2,
                            'label': label,
                            'name': name
                        })
                
                # Close button - expanded detection
                if elem_type == 'XCUIElementTypeButton' and (
                    'close' in label.lower() or 
                    'close' in name.lower() or
                    name == 'Player_Close_Button' or
                    'dismiss' in label.lower() or
                    label == 'X' or
                    '×' in label
                ):
                    elements['close_button'] = {
                        'x': float(elem.get('x', 0)) + float(elem.get('width', 0)) / 2,
                        'y': float(elem.get('y', 0)) + float(elem.get('height', 0)) / 2
                    }
                    logger.debug(f"✓ Found close button: {label or name}")
                
                # Play button
                elif elem_type == 'XCUIElementTypeButton' and (
                    name == 'Player_Play_Button' or 
                    (label.lower() == 'play' and 'airplay' not in label.lower())
                ):
                    elements['play_button'] = {
                        'x': float(elem.get('x', 0)) + float(elem.get('width', 0)) / 2,
                        'y': float(elem.get('y', 0)) + float(elem.get('height', 0)) / 2
                    }
                    logger.debug(f"✓ Found play button: {label or name}")
                
                # Pause button
                elif elem_type == 'XCUIElementTypeButton' and (
                    name == 'Player_Pause_Button' or 
                    label.lower() == 'pause'
                ):
                    elements['pause_button'] = {
                        'x': float(elem.get('x', 0)) + float(elem.get('width', 0)) / 2,
                        'y': float(elem.get('y', 0)) + float(elem.get('height', 0)) / 2
                    }
                    logger.debug(f"✓ Found pause button: {label or name}")
                
                # Timestamp
                elif elem_type == 'XCUIElementTypeStaticText' and name == 'Player_Progress_Label':
                    if '/' in label:
                        parts = label.split('/')
                        if len(parts) == 2:
                            time_match = re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)', parts[0])
                            if time_match:
                                elements['timestamp'] = time_match.group(1).strip()
                                logger.debug(f"✓ Found timestamp: {elements['timestamp']}")
            
            # Fallback: If no close button found by label, use top-left button
            if not elements['close_button'] and top_left_buttons:
                elements['close_button'] = {
                    'x': top_left_buttons[0]['x'],
                    'y': top_left_buttons[0]['y']
                }
                logger.debug(f"✓ Using top-left button as close: {top_left_buttons[0].get('label') or top_left_buttons[0].get('name')}")
            
            # Log what we found
            logger.debug(f"🎬 Video scan results: close={elements['close_button'] is not None}, "
                        f"play={elements['play_button'] is not None}, pause={elements['pause_button'] is not None}, "
                        f"timestamp={elements['timestamp']}")
            
            if not any([elements['close_button'], elements['play_button'], elements['pause_button']]):
                logger.warning(f"⚠️ No video controls found. Buttons seen: {buttons_found[:5]}")
            
            return elements
            
        except Exception as e:
            logger.error(f"❌ Video scan error: {e}")
            return None
    
    def _video_ensure_controls_visible(self):
        """Ensure video controls are visible"""
        elements = self._video_scan_elements()
        
        # Check if controls are visible (at least one control element should be present)
        controls_visible = (elements and 
                           (elements['play_button'] or 
                            elements['pause_button'] or 
                            elements['timestamp'] or 
                            elements['close_button']))
        
        if not controls_visible:
            logger.debug("🎬 Controls not visible, revealing...")
            self._video_reveal_controls()
            time.sleep(1.0)  # Give more time for animation
            elements = self._video_scan_elements()
            
        return elements

# Create global instance
controller_service = ControllerService()
