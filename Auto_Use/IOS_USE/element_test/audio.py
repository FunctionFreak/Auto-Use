import subprocess
import time

class PureAudioDetector:
    def __init__(self):
        pass
        
    def detect_audio(self, duration=3):
        """Dead simple audio detection - just count core processing events"""
        print(f"🎧 Monitoring audio processing for {duration} seconds...")
        
        process = subprocess.Popen(
            ['idevicesyslog'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        audio_events = 0
        start_time = time.time()
        
        try:
            while time.time() - start_time < duration:
                line = process.stdout.readline()
                
                # Count core audio processing events only
                if any(indicator in line for indicator in [
                    "ConvertOutput",
                    "EnqueueBuffer", 
                    "segPumpSendMediaCallback",
                    "AudioQueueObject"
                ]):
                    audio_events += 1
                    
        finally:
            process.terminate()
        
        # Calculate rate
        events_per_second = audio_events / duration
        
        # MUCH higher threshold based on your test results:
        # Playing: 192.6/sec, 144.8/sec  
        # Paused: 0.0/sec
        # So use 120+ events/sec as threshold
        is_playing = events_per_second > 120
        
        return {
            "audio_detected": is_playing,
            "events_per_second": events_per_second
        }

def is_audio_playing():
    detector = PureAudioDetector()
    result = detector.detect_audio(3)
    
    print("\n" + "="*50)
    if result["audio_detected"]:
        print("✅ AUDIO PLAYING")
    else:
        print("❌ NO AUDIO")
    
    print(f"Activity: {result['events_per_second']:.1f} events/sec")
    print(f"Threshold: 120+ events/sec for detection")
    print("="*50)
    
    return result["audio_detected"]

if __name__ == "__main__":
    is_audio_playing()