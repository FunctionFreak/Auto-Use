import requests
import json
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw, ImageFont
import io
import base64
import os
import time
from agent_core.controller.service import controller_service
from agent_core.vault.service import vault_service

# Debug folder to save scans
DEBUG = False  # Set to True to save files to debug folders

# Configuration embedded in code
config = {
    "element_types": {
        "button": {
            "type": "XCUIElementTypeButton",
            "enabled": True
        },
        "search_field": {
            "type": "XCUIElementTypeSearchField",
            "enabled": True
        },
        "text_field": {
            "type": "XCUIElementTypeTextField",
            "enabled": True
        },
        "switch": {
            "type": "XCUIElementTypeSwitch",
            "enabled": True
        },
        "application": {
            "type": "XCUIElementTypeApplication",
            "enabled": True
        },
        "icon": {
            "type": "XCUIElementTypeIcon",
            "enabled": True
        },
        "page_indicator": {
            "type": "XCUIElementTypePageIndicator", 
            "enabled": True
        },
        "scroll_view": {
            "type": "XCUIElementTypeScrollView",
            "enabled": True
        },
        "secure_text_field": {
            "type": "XCUIElementTypeSecureTextField",
            "enabled": True
        },
        "other": {
            "type": "XCUIElementTypeOther",
            "enabled": True
        },
        "slider": {
            "type": "XCUIElementTypeSlider",
            "enabled": True
        },
        "static_text": {
            "type": "XCUIElementTypeStaticText",
            "enabled": True
        }
    },
    "only_visible": True
}

# WebDriverAgent endpoint
wda_url = "http://localhost:8100"

class UIElementScanner:
    """Scanner for iPhone UI elements using WebDriverAgent"""
    
    def __init__(self, config=None):
        self.config = config or {}
        self.elements_mapping = {}
        
        # Store scan data in memory
        self.element_tree_text = ""
        self.image_base64 = None
        
        # Create debug directories if DEBUG is enabled
        if DEBUG:
            os.makedirs("debug/element", exist_ok=True)
            os.makedirs("debug/screenshot", exist_ok=True)
    
    def scan_elements(self):
        """Scan iPhone UI elements and capture screenshot"""
        try:
            # Get the page source from WDA
            response = requests.get(f"{wda_url}/source")
            
            if response.status_code == 200:
                # Parse JSON and get XML
                xml_string = response.json()['value']
                root = ET.fromstring(xml_string)
                
                # Get enabled element types
                enabled_types = [v['type'] for k, v in config['element_types'].items() if v['enabled']]
                
                # List to store found elements
                found_elements = []
                
                # Get the main window dimensions from the XML for reference
                main_window = root.find(".//XCUIElementTypeWindow[@visible='true']")
                if main_window is not None:
                    xml_width = int(main_window.get('width', '0'))
                    xml_height = int(main_window.get('height', '0'))
                
                # Function to extract elements using XML tree hierarchy
                def extract_elements(element, current_depth=0):
                    """
                    Extract elements recursively, preserving XML tree structure.
                    This correctly handles scrollviews where children may be off-screen or beyond visible bounds.
                    """
                    element_type = element.get('type', '')
                    
                    # Track if we should increment depth for children
                    element_added = False
                    
                    # Check if this element should be included
                    if element_type in enabled_types:
                        # Filter "other" elements to only include MainTabBar or Tab items
                        should_add = True
                        if element_type == "XCUIElementTypeOther":
                            label = element.get('label', '').lower()
                            name = element.get('name', '').lower()
                            if "maintabbar" not in label and "maintabbar" not in name and "tab" not in label and "tab" not in name and "text size" not in label:
                                should_add = False
                        
                        # Check visibility if required and add element
                        if should_add and (not config['only_visible'] or element.get('visible', 'false') == 'true'):
                            # Get element info with depth from XML tree
                            info = {
                                'type': element_type,
                                'label': element.get('label', ''),
                                'name': element.get('name', ''),
                                'value': element.get('value', ''),
                                'x': float(element.get('x', '0')),
                                'y': float(element.get('y', '0')),
                                'width': float(element.get('width', '0')),
                                'height': float(element.get('height', '0')),
                                'depth': current_depth
                            }
                            found_elements.append(info)
                            element_added = True
                    
                    # Always process children (whether element was added or not)
                    # Only increment depth if this element was actually added
                    next_depth = current_depth + 1 if element_added else current_depth
                    for child in element:
                        extract_elements(child, next_depth)
                
                # Start extraction from root using XML tree structure
                extract_elements(root, 0)
                
                # Create virtual tick marks for adjustable elements (like sliders)
                def create_virtual_ticks(element, num_ticks=5):
                    """Create virtual tick marks for adjustable elements like sliders"""
                    virtual_ticks = []
                    
                    # Check if this is an adjustable element (text size slider)
                    label = element['label'].lower()
                    if 'text size' in label or element['type'] == 'XCUIElementTypeSlider':
                        # Calculate tick positions along the width
                        x_start = element['x']
                        y_start = element['y']
                        width = element['width']
                        height = element['height']
                        
                        # Tick dimensions - small enough to fit inside
                        tick_width = 8.0
                        tick_height = min(height, 20.0)  # Don't exceed parent height
                        
                        # Divide into equal segments, keeping ticks inside bounds
                        # Account for tick width so they don't overflow
                        usable_width = width - tick_width
                        spacing = usable_width / (num_ticks - 1) if num_ticks > 1 else 0
                        
                        for i in range(num_ticks):
                            # Center ticks vertically within slider
                            tick_x = x_start + (i * spacing)
                            tick_y = y_start + (height - tick_height) / 2
                            
                            tick_info = {
                                'type': 'XCUIElementTypeOther',  # Keep as Other for consistency
                                'label': f'Text size tick {i+1}',
                                'name': f'tick_{i+1}',
                                'value': f'Position {i+1}',
                                'x': tick_x,
                                'y': tick_y,
                                'width': tick_width,
                                'height': tick_height,
                                'depth': element['depth'] + 1,  # One level deeper than parent
                                'is_virtual': True  # Mark as virtual
                            }
                            virtual_ticks.append(tick_info)
                    
                    return virtual_ticks
                
                # Insert virtual ticks after their parent elements
                expanded_elements = []
                for elem in found_elements:
                    expanded_elements.append(elem)
                    elem['is_virtual'] = False  # Mark real elements
                    
                    # Create and insert virtual ticks for adjustable elements
                    virtual_ticks = create_virtual_ticks(elem)
                    expanded_elements.extend(virtual_ticks)
                
                # Replace found_elements with expanded list
                found_elements = expanded_elements
                
                # Clear previous mappings
                self.elements_mapping = {}
                
                # Build mapping for controller
                for index, elem in enumerate(found_elements, 1):
                    label = elem['label'] or elem['name'] or 'no_label'
                    # Clean up long labels - replace newlines with spaces and truncate if too long
                    label = label.replace('\n', ' ').replace('\r', '')
                    if len(label) > 50:
                        label = label[:47] + "..."
                    
                    bounds = {
                        'x': int(elem['x']),
                        'y': int(elem['y']),
                        'width': int(elem['width']),
                        'height': int(elem['height'])
                    }
                    
                    elem_data = {
                        'type': elem['type'].split('XCUIElementType')[-1],
                        'name': label,
                        'number': index,
                        'depth': elem['depth'],
                        'bounds': bounds
                    }
                    
                    # Add value if it exists
                    if elem['value']:
                        elem_data['value'] = elem['value']
                    
                    # Store mapping for controller
                    self.elements_mapping[str(index)] = elem_data
                
                # Update controller with element mappings
                controller_service.update_elements(self.elements_mapping)
                
                # Build element tree text in memory
                element_lines = []
                for index, elem in enumerate(found_elements, 1):
                    label = elem['label'] or elem['name'] or 'no_label'
                    # Clean up long labels - replace newlines with spaces and truncate if too long
                    label = label.replace('\n', ' ').replace('\r', '')
                    if len(label) > 50:
                        label = label[:47] + "..."
                    
                    value = elem['value']
                    element_type = elem['type'].split('XCUIElementType')[-1].lower()
                    indent = "    " * elem['depth']  # 4 spaces per depth level
                    element_lines.append(f'{indent}[{index}]<type="{element_type}", label="{label}", value="{value}", x="{elem["x"]}", y="{elem["y"]}", w="{elem["width"]}", h="{elem["height"]}" />\n')
                
                # Store in memory
                self.element_tree_text = ''.join(element_lines)
                # Send element tree to vault
                vault_service.update_element_tree(self.element_tree_text)
                
                
                # Take screenshot
                screenshot_response = requests.get(f"{wda_url}/screenshot")
                
                if screenshot_response.status_code == 200:
                    # Get base64 image
                    screenshot_data = screenshot_response.json()
                    image_base64 = screenshot_data['value']
                    
                    # Decode base64 to image
                    image_bytes = base64.b64decode(image_base64)
                    image = Image.open(io.BytesIO(image_bytes))
                    
                    # Get actual image dimensions
                    img_width, img_height = image.size
                    
                    # Calculate scale factors
                    if 'xml_width' in locals() and xml_width > 0:
                        scale_x = img_width / xml_width
                        scale_y = img_height / xml_height
                    else:
                        # iPhone 12 fallback scaling
                        if xml_width < 400:
                            scale_x = img_width / 390
                            scale_y = img_height / 844
                        else:
                            scale_x = img_width / xml_width
                            scale_y = img_height / xml_height
                    
                    # Create drawing context
                    draw = ImageDraw.Draw(image)
                    
                    # Font setup - matching test2.py
                    font = None
                    font_size = 24  # Same as test2.py
                    for path in ["/System/Library/Fonts/Helvetica.ttc", "arial.ttf"]:
                        try:
                            font = ImageFont.truetype(path, font_size)
                            break
                        except:
                            continue
                    if not font:
                        font = ImageFont.load_default()
                    
                    # Draw orange boxes on detected elements with scaled coordinates
                    for i, elem in enumerate(found_elements, 1):
                        # Apply scale factors to coordinates
                        x = elem['x'] * scale_x
                        y = elem['y'] * scale_y
                        width = elem['width'] * scale_x
                        height = elem['height'] * scale_y
                        
                        # Round to integers for drawing
                        x, y, width, height = int(x), int(y), int(width), int(height)
                        
                        # Skip elements that are too small or have invalid dimensions
                        if width <= 0 or height <= 0:
                            continue
                        
                        # Draw orange rectangle (box outline) with thicker lines
                        for offset in range(3):
                            draw.rectangle(
                                [x - offset, y - offset, x + width + offset, y + height + offset],
                                outline='orange',
                                width=1
                            )
                        
                        # Add element number with background - improved font and styling
                        text = f"[{i}]"
                        bbox = draw.textbbox((0, 0), text, font=font)
                        tw = bbox[2] - bbox[0]
                        th = bbox[3] - bbox[1]
                        
                        # Center the text inside the box at the top (original position)
                        text_x = x + (width // 2) - (tw // 2)  # Center horizontally
                        text_y = y + 5  # Position inside the box, 5 pixels from top
                        
                        # White background with padding
                        padding = 3
                        draw.rectangle([(text_x - padding, text_y - padding), 
                                       (text_x + tw + padding, text_y + th + padding)],
                                     fill='white', outline='#FF8800', width=2)
                        draw.text((text_x, text_y), text, fill='black', font=font)
                    
                    # Convert image to base64 for memory storage
                    buffered = io.BytesIO()
                    image.save(buffered, format="PNG")
                    self.image_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                    
                    # Save to debug folder ONLY if DEBUG is enabled
                    if DEBUG:
                        timestamp = int(time.time())
                        debug_element_file = f"debug/element/ui_elements_{timestamp}.txt"
                        debug_screenshot_file = f"debug/screenshot/ui_elements_screenshot_{timestamp}.png"
                        
                        # Save element tree to debug folder
                        with open(debug_element_file, 'w', encoding='utf-8') as f:
                            f.write(self.element_tree_text)
                        
                        # Save annotated screenshot to debug folder
                        image.save(debug_screenshot_file)
                    
                else:
                    print(f"Failed to take screenshot: {screenshot_response.status_code}")
                
                # Show preview
                print(f"Elements found: {len(found_elements)}")
            
            else:
                print(f"Failed to get source: {response.status_code}")
                
        except Exception as e:
            print(f"Error: {e}")
    
    def get_scan_data(self):
        """Get scan data for AgentService - returns element tree text and base64 image from memory"""
        return self.element_tree_text, self.image_base64

# For compatibility
ELEMENT_CONFIG = {}

if __name__ == "__main__":
    scanner = UIElementScanner()
    scanner.scan_elements()