import os
import base64
from google import genai
from google.genai import types
from typing import Dict, Any, Optional

class GoogleGeminiProvider:
    """Google Gemini API provider for LLM interactions"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = genai.Client(api_key=api_key)
        
    def send_request(self, messages: list, model: str, screenshot_base64: Optional[str] = None) -> Dict[str, Any]:
        """Send request to Google Gemini API"""
        
        # Create config
        config = types.GenerateContentConfig(
            temperature=0.1,
            top_p=1,
            seed=42,
            max_output_tokens=65535,
            thinking_config=types.ThinkingConfig(
                thinking_budget=0  # Disable thinking
            )
        )
        
        # Prepare content - Gemini uses a different format
        # Convert messages to Gemini format
        contents = self._convert_messages_to_gemini_format(messages, screenshot_base64)
        
        try:
            response = self.client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
            
            # Convert response to OpenAI-like format for compatibility
            return {
                'choices': [{
                    'message': {
                        'content': response.text
                    }
                }]
            }
        except Exception as e:
            error_msg = f"Google Gemini API request failed: {str(e)}"
            raise Exception(error_msg)
    
    def _convert_messages_to_gemini_format(self, messages: list, screenshot_base64: Optional[str] = None):
        """Convert OpenAI-style messages to Gemini format with image support"""
        
        prompt_parts = []
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            if role == "system":
                prompt_parts.append(f"System: {content}")
            elif role == "user":
                prompt_parts.append(f"User: {content}")
            elif role == "assistant":
                prompt_parts.append(f"Assistant: {content}")
        
        full_prompt = "\n\n".join(prompt_parts)
        
        # If screenshot is provided, include it as a Part with the text
        if screenshot_base64:
            # Create a list of parts: text + image
            content_parts = [
                types.Part(text=full_prompt),
                types.Part(
                    inline_data=types.Blob(
                        mime_type="image/png",
                        data=base64.b64decode(screenshot_base64)
                    )
                )
            ]
            return content_parts
        
        # Return just text if no image
        return full_prompt

