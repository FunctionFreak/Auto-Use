# Copyright 2026 Ashish Yadav — Auto-Use

"""
videoplayer.py - Full-screen video playback control via WebDriverAgent.

DRM-protected players black out screenshots, so the agent cannot see the
video surface. This service works from the accessibility source instead:
it scans the player overlay for its control buttons (close/play/pause) and
progress label, reveals the controls with a tap when hidden, and drives
playback from there.
"""

import requests
import time

# (connect, read) seconds for the WebDriverAgent source dump — same bound the
# scanner uses at Auto_Use/ios/tree/element.py:57. Without it `requests` blocks
# forever, and a wedged WDA takes the whole agent down with it.
WDA_SOURCE_TIMEOUT = (5, 60)
import logging
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


class VideoPlayerService:
    """Controls the full-screen video player through the controller session"""

    def __init__(self, controller_service):
        # Shares the WDA endpoint + session handling with the controller service
        self.controller_service = controller_service

    @property
    def wda_url(self):
        return self.controller_service.wda_url

    def get_session(self):
        return self.controller_service.get_session()

    def close(self):
        """Close the video player"""
        try:
            # Ensure controls are visible
            elements = self._ensure_controls_visible()
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

    def check_streaming(self):
        """Check if video is currently streaming/playing"""
        try:
            # Get initial state
            elements1 = self._ensure_controls_visible()
            if not elements1:
                return False

            # If no timestamp, check button state
            if not elements1['timestamp']:
                return elements1['pause_button'] is not None

            timestamp1 = elements1['timestamp']

            # Wait and check again
            time.sleep(4)

            elements2 = self._ensure_controls_visible()
            if not elements2 or not elements2['timestamp']:
                return elements2 and elements2['pause_button'] is not None

            timestamp2 = elements2['timestamp']

            # Video is streaming if timestamp changed
            return timestamp1 != timestamp2

        except Exception as e:
            logger.error(f"Video streaming check error: {e}")
            return False

    def pause(self):
        """Pause the video"""
        try:
            elements = self._ensure_controls_visible()
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
            elements_after = self._ensure_controls_visible()

            return elements_after and elements_after['play_button'] is not None

        except Exception as e:
            logger.error(f"Video pause error: {e}")
            return False

    def play(self):
        """Play the video"""
        try:
            elements = self._ensure_controls_visible()
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
            elements_after = self._ensure_controls_visible()

            return elements_after and elements_after['pause_button'] is not None

        except Exception as e:
            logger.error(f"Video play error: {e}")
            return False

    def _reveal_controls(self):
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

    def _scan_elements(self):
        """Scan video player elements"""
        try:
            response = requests.get(f"{self.wda_url}/source", timeout=WDA_SOURCE_TIMEOUT)
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

    def _ensure_controls_visible(self):
        """Ensure video controls are visible"""
        elements = self._scan_elements()

        # Check if controls are visible (at least one control element should be present)
        controls_visible = (elements and
                           (elements['play_button'] or
                            elements['pause_button'] or
                            elements['timestamp'] or
                            elements['close_button']))

        if not controls_visible:
            logger.debug("🎬 Controls not visible, revealing...")
            self._reveal_controls()
            time.sleep(1.0)  # Give more time for animation
            elements = self._scan_elements()

        return elements
