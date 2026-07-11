from .google.service import GoogleGeminiProvider
from .google.view import get_model_info as get_google_model_info
import os
from dotenv import load_dotenv
from typing import Optional

# Load environment variables
load_dotenv()

class LLMManager:
    """Manager to route requests to Google Gemini provider"""
    
    def __init__(self, provider: str = "google", model: str = "gemini-2.5-flash"):
        self.provider = "google"  # Only Google Gemini supported
        self.model_short_name = model
        
        # Get model info
        model_info = get_google_model_info(model)
        
        self.model = model_info["api_name"]
        self.has_vision = model_info["vision"]
        self.display_name = model_info["display_name"]
        
        self.provider_instance = self._initialize_provider()
        
    def _initialize_provider(self):
        """Initialize Google Gemini provider"""
        api_key = os.getenv('GOOGLE_API_KEY')
        if not api_key:
            # Fallback to hardcoded key if not in env
            api_key = "AIzaSyDRPtU5NstzYy3alkGBMjtoZ8kdVg8qH6c"
        return GoogleGeminiProvider(api_key)
    
    def send_request(self, messages: list, screenshot_base64: Optional[str] = None):
        """Send request to Google Gemini"""
        response = self.provider_instance.send_request(messages, self.model, screenshot_base64)
        
        # Extract the assistant's response
        return response['choices'][0]['message']['content']
    
    def get_model_name(self) -> str:
        """Get the current model name"""
        return self.model
    
    def get_provider_name(self) -> str:
        """Get the current provider name"""
        return self.provider