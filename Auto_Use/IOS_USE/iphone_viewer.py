#!/usr/bin/env python3
"""
iphone_viewer.py - Live iPhone Screen Viewer
Streams iPhone screen at 30 FPS via WebDriverAgent with optimized polling
"""

import cv2
import numpy as np
import requests
import base64
import time
import threading
from queue import Queue
from io import BytesIO
from PIL import Image
import sys
import platform

class iPhoneViewer:
    def __init__(self):
        self.wda_url = "http://localhost:8100"
        self.running = False
        self.frame_queue = Queue(maxsize=2)
        self.fps = 30
        self.frame_time = 1.0 / self.fps
        self.is_macos = platform.system() == 'Darwin'
        
    def capture_screenshot(self):
        """Capture screenshot from WDA"""
        try:
            response = requests.get(f"{self.wda_url}/screenshot", timeout=1)
            if response.status_code == 200:
                img_base64 = response.json()['value']
                img_bytes = base64.b64decode(img_base64)
                
                # Convert to numpy array for OpenCV
                pil_img = Image.open(BytesIO(img_bytes))
                frame = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
                return frame
        except:
            pass
        return None
    
    def optimized_capture_thread(self):
        """Optimized capture thread with session reuse"""
        # Reuse HTTP session for better performance
        session = requests.Session()
        
        while self.running:
            start_time = time.time()
            
            try:
                # Use session for connection pooling
                response = session.get(f"{self.wda_url}/screenshot", timeout=1)
                if response.status_code == 200:
                    img_base64 = response.json()['value']
                    img_bytes = base64.b64decode(img_base64)
                    
                    # Fast decode using OpenCV directly
                    nparr = np.frombuffer(img_bytes, np.uint8)
                    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                    
                    if frame is not None:
                        # Drop old frames if queue is full
                        if self.frame_queue.full():
                            try:
                                self.frame_queue.get_nowait()
                            except:
                                pass
                        self.frame_queue.put(frame)
            except:
                pass
            
            # Maintain target FPS
            elapsed = time.time() - start_time
            sleep_time = max(0, self.frame_time - elapsed)
            time.sleep(sleep_time)
    
    def capture_thread(self):
        """Thread for continuous capture (fallback method)"""
        while self.running:
            start_time = time.time()
            
            frame = self.capture_screenshot()
            if frame is not None:
                # Drop old frames if queue is full
                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                    except:
                        pass
                self.frame_queue.put(frame)
            
            # Maintain target FPS
            elapsed = time.time() - start_time
            sleep_time = max(0, self.frame_time - elapsed)
            time.sleep(sleep_time)
    
    def start_background(self):
        """Start viewer in background mode (headless)"""
        print("📱 iPhone Screen Viewer (Background Mode)")
        
        # Test connection
        try:
            response = requests.get(f"{self.wda_url}/status", timeout=2)
            if response.status_code != 200:
                print("❌ Cannot connect to iPhone (WDA)")
                return
        except:
            print("❌ WDA not running at http://localhost:8100")
            return
        
        self.running = True
        
        # Start optimized capture thread
        capture_t = threading.Thread(target=self.optimized_capture_thread)
        capture_t.daemon = True
        capture_t.start()
        
        print("✅ Viewer running in background (Optimized polling @ 30 FPS)")
        
    def start(self):
        """Start the viewer with display"""
        print("📱 iPhone Screen Viewer")
        print("Press 'q' to quit")
        
        # Test connection
        try:
            response = requests.get(f"{self.wda_url}/status", timeout=2)
            if response.status_code != 200:
                print("❌ Cannot connect to iPhone (WDA)")
                return
        except:
            print("❌ WDA not running at http://localhost:8100")
            return
        
        self.running = True
        
        # Start optimized capture thread
        capture_t = threading.Thread(target=self.optimized_capture_thread)
        capture_t.daemon = True
        capture_t.start()
        
        print("✅ Capturing at 30 FPS (optimized)")
        
        # Display loop
        cv2.namedWindow("iPhone Screen", cv2.WINDOW_NORMAL)
        last_frame = None
        
        # FPS calculation
        frame_count = 0
        fps_start_time = time.time()
        actual_fps = 0
        
        while True:
            # Get latest frame
            frame = None
            try:
                frame = self.frame_queue.get(timeout=0.1)
                last_frame = frame
                frame_count += 1
            except:
                frame = last_frame
            
            # Calculate actual FPS every second
            if time.time() - fps_start_time >= 1.0:
                actual_fps = frame_count / (time.time() - fps_start_time)
                frame_count = 0
                fps_start_time = time.time()
            
            if frame is not None:
                # Calculate scale to fit screen nicely
                height, width = frame.shape[:2]
                max_height = 800
                if height > max_height:
                    scale = max_height / height
                    new_width = int(width * scale)
                    new_height = int(height * scale)
                    frame = cv2.resize(frame, (new_width, new_height))
                
                # Show actual FPS
                cv2.putText(frame, f"FPS: {actual_fps:.1f}", (10, 30), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                
                cv2.imshow("iPhone Screen", frame)
            
            # Check for quit
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
        
        self.running = False
        cv2.destroyAllWindows()
        print("✅ Viewer closed")
    
    def stop(self):
        """Stop the viewer"""
        self.running = False

# Global viewer instance
viewer = iPhoneViewer()

def start():
    """Start the viewer"""
    viewer.start()

def start_background():
    """Start in background mode"""
    viewer.start_background()

if __name__ == "__main__":
    start()