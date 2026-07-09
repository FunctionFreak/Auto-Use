import requests
import json
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw, ImageFont
import io
import base64
import sys
import os

# Get the directory where this script is located
script_dir = os.path.dirname(os.path.abspath(__file__))

# Add both current directory and controller_test to path
sys.path.append('.')
sys.path.append('controller_test')

try:
    from controller import controller
except ImportError:
    try:
        from controller_test.controller import controller
    except ImportError:
        print("Error: Could not import controller module")
        print("Make sure controller.py is in the current directory or controller_test/")
        sys.exit(1)

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
        }
    },
    "only_visible": True
}

# WebDriverAgent endpoint
wda_url = "http://localhost:8100"

def scan_and_display():
    """Scan the screen and display elements with numbered boxes"""
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
            element_file = os.path.join(script_dir, 'element.txt')
            with open(element_file, 'w', encoding='utf-8') as f:
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
                    
            print(f"Found {len(found_elements)} element(s)")
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
                screenshot_file = os.path.join(script_dir, 'screenshot_with_boxes.png')
                image.save(screenshot_file)
                print("Screenshot saved as screenshot_with_boxes.png")
                
            else:
                print(f"Failed to take screenshot: {screenshot_response.status_code}")
                return None
            
            # Show preview
            print("\nElements found:")
            for i, elem in enumerate(found_elements, 1):
                label = elem['label'] or elem['name'] or 'no_label'
                element_type = elem['type'].split('XCUIElementType')[-1].lower()
                print(f"[{i}] {element_type}: {label}")
            
            return found_elements
            
        else:
            print(f"Failed to get source: {response.status_code}")
            return None
            
    except Exception as e:
        print(f"Error: {e}")
        return None

def interactive_mode():
    """Run the interactive mode where user can click and type"""
    print("🎮 Interactive Element Control")
    print("=" * 40)
    print("Commands:")
    print("  click,<number>             - Click element (e.g., 'click,8')")
    print("  type,<number>,<text>       - Type text (e.g., 'type,4,hello world')")
    print("  scroll,<number>,<direction> - Scroll element (e.g., 'scroll,7,up')")
    print("                               Directions: up, down, left, right")
    print("  scan                       - Rescan screen")
    print("  quit                       - Exit")
    print("=" * 40)
    
    while True:
        # Scan and display current screen
        elements = scan_and_display()
        if not elements:
            print("Failed to scan screen. Exiting.")
            break
        
        print("\n" + "-" * 40)
        command = input("Command: ").strip()
        
        if command.lower() == 'quit':
            print("Exiting...")
            break
        
        elif command.lower() == 'scan':
            print("Rescanning...")
            continue
        
        elif command.startswith('click,'):
            try:
                # Parse click command
                parts = command.split(',')
                elem_num = int(parts[1])
                
                if 1 <= elem_num <= len(elements):
                    print(f"Clicking element [{elem_num}]...")
                    result = controller.click(elem_num)
                    if result:
                        print("✓ Clicked! Rescanning...")
                    else:
                        print("⚠️ Click returned False. Check WDA connection.")
                else:
                    print(f"❌ Invalid element number. Please choose 1-{len(elements)}")
            except (ValueError, IndexError) as e:
                print(f"❌ Invalid format. Use: click,<number>. Error: {e}")
            except Exception as e:
                print(f"❌ Click failed: {e}")
        
        elif command.startswith('type,'):
            try:
                # Parse type command
                parts = command.split(',', 2)  # Split only first 2 commas
                if len(parts) >= 3:
                    elem_num = int(parts[1])
                    text = parts[2]
                    
                    if 1 <= elem_num <= len(elements):
                        print(f"Typing in element [{elem_num}]: '{text}'")
                        result = controller.type_text(elem_num, text)
                        if result:
                            print("✓ Typed! Rescanning...")
                        else:
                            print("⚠️ Type returned False. Check WDA connection.")
                    else:
                        print(f"❌ Invalid element number. Please choose 1-{len(elements)}")
                else:
                    print("❌ Invalid format. Use: type,<number>,<text>")
            except (ValueError, IndexError):
                print("❌ Invalid format. Use: type,<number>,<text>")
            except Exception as e:
                print(f"❌ Type failed: {e}")
        
        elif command.startswith('scroll,'):
            try:
                # Parse scroll command
                parts = command.split(',')
                if len(parts) >= 3:
                    elem_num = int(parts[1])
                    direction = parts[2].strip()
                    
                    if 1 <= elem_num <= len(elements):
                        if direction.lower() in ['up', 'down', 'left', 'right']:
                            print(f"Scrolling element [{elem_num}] {direction}...")
                            result = controller.scroll(elem_num, direction)
                            if result:
                                print("✓ Scrolled! Rescanning...")
                            else:
                                print("⚠️ Scroll returned False. Check WDA connection.")
                        else:
                            print("❌ Invalid direction. Use: up, down, left, or right")
                    else:
                        print(f"❌ Invalid element number. Please choose 1-{len(elements)}")
                else:
                    print("❌ Invalid format. Use: scroll,<number>,<direction>")
            except (ValueError, IndexError):
                print("❌ Invalid format. Use: scroll,<number>,<direction>")
            except Exception as e:
                print(f"❌ Scroll failed: {e}")
        
        else:
            print("❌ Unknown command. Use: click,<n> | type,<n>,<text> | scroll,<n>,<direction> | scan | quit")

# Main execution
if __name__ == "__main__":
    interactive_mode()
