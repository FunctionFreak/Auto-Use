import requests
import json
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw, ImageFont
import io
import base64

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
        
        # Function to extract elements by type
        def extract_elements(element):
            # Check if element type is enabled
            element_type = element.get('type', '')
            
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
                    # Get element info
                    info = {
                        'type': element_type,
                        'label': element.get('label', ''),
                        'name': element.get('name', ''),
                        'value': element.get('value', ''),
                        'x': float(element.get('x', '0')),
                        'y': float(element.get('y', '0')),
                        'width': float(element.get('width', '0')),
                        'height': float(element.get('height', '0')),
                        'depth': 0  # Will be calculated later based on containment
                    }
                    found_elements.append(info)
            
            # Process children
            for child in element:
                extract_elements(child)
        
        # Function to check if element A is contained within element B
        def is_contained(elem_a, elem_b):
            """Check if elem_a is spatially inside elem_b"""
            # Add tolerance for slight boundary overlaps (iOS accessibility quirks)
            tolerance = 5
            
            a_x1, a_y1 = elem_a['x'], elem_a['y']
            a_x2, a_y2 = a_x1 + elem_a['width'], a_y1 + elem_a['height']
            
            b_x1, b_y1 = elem_b['x'], elem_b['y']
            b_x2, b_y2 = b_x1 + elem_b['width'], b_y1 + elem_b['height']
            
            # A is inside B if all corners of A are within B's bounds (with tolerance)
            return (a_x1 >= b_x1 - tolerance and a_y1 >= b_y1 - tolerance and 
                    a_x2 <= b_x2 + tolerance and a_y2 <= b_y2 + tolerance)
        
        # Start extraction - collect all elements first
        extract_elements(root)
        
        # Now calculate depth based on spatial containment
        for i, elem in enumerate(found_elements):
            depth = 0
            # Find all elements that contain this element
            for j, potential_parent in enumerate(found_elements):
                if i != j:  # Don't compare with itself
                    # Check if elem is contained within potential_parent
                    if is_contained(elem, potential_parent):
                        # Find the smallest container (most specific parent)
                        # Count how many elements contain this one
                        depth += 1
            elem['depth'] = depth
        
        # Save to file
        with open('element.txt', 'w', encoding='utf-8') as f:
            for index, elem in enumerate(found_elements, 1):
                label = elem['label'] or elem['name'] or 'no_label'
                # Clean up long labels - replace newlines with spaces and truncate if too long
                label = label.replace('\n', ' ').replace('\r', '')
                if len(label) > 50:
                    label = label[:47] + "..."
                
                value = elem['value']
                element_type = elem['type'].split('XCUIElementType')[-1].lower()
                indent = "    " * elem['depth']  # 4 spaces per depth level
                f.write(f'{indent}[{index}]<type="{element_type}", label="{label}", value="{value}", x="{elem["x"]}", y="{elem["y"]}", w="{elem["width"]}", h="{elem["height"]}" />\n')
                
                print(f"Found {len(found_elements)} button(s)")
                print("Saved to element.txt")
        
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
            
            # Save the image with boxes
            image.save('screenshot_with_boxes.png')
            print("Screenshot saved as screenshot_with_boxes.png")
            
        else:
            print(f"Failed to take screenshot: {screenshot_response.status_code}")
        
        # Show preview
        print("\nButtons found:")
        for i, elem in enumerate(found_elements, 1):
            label = elem['label'] or elem['name'] or 'no_label'
            print(f"[{i}]<type=button label={label} />")
        
    else:
        print(f"Failed to get source: {response.status_code}")
        
except Exception as e:
    print(f"Error: {e}")