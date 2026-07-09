import json
import re

class AgentResponseFormatter:
    """Formats agent JSON responses for terminal display"""
    
    # Emoji mappings for each field
    FIELD_EMOJIS = {
        "thinking": "🧠 Thinking",
        "verdict_last_action": "⚖️  Verdict Last Action", 
        "image_observation": "👁️  Image Observation",
        "memory": "💾 Memory",
        "current_goal": "🎯 Current Goal",
        "action": "⚡ Action"
    }
    
    @staticmethod
    def normalize_response(raw_response: str) -> str:
        """
        Normalize LLM response to ensure consistent format.
        Always returns response in ```json\n{...}\n``` format.
        """
        try:
            # First, try to extract JSON from various possible formats
            json_data = None
            
            # Case 1: Response is already in ```json ... ``` format
            json_match = re.search(r'```json\s*(.*?)\s*```', raw_response, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
                json_data = json.loads(json_str)
            
            # Case 2: Response is raw JSON (no markdown wrapper)
            if not json_data:
                try:
                    # Try to parse the entire response as JSON
                    json_data = json.loads(raw_response.strip())
                except json.JSONDecodeError:
                    # If that fails, try to find JSON object in the response
                    json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw_response, re.DOTALL)
                    if json_match:
                        json_str = json_match.group(0)
                        json_data = json.loads(json_str)
            
            # Case 3: Response has JSON but with extra text
            if not json_data:
                # Look for JSON-like structure more aggressively
                lines = raw_response.split('\n')
                json_start = -1
                json_end = -1
                brace_count = 0
                
                for i, line in enumerate(lines):
                    if '{' in line and json_start == -1:
                        json_start = i
                    if json_start != -1:
                        brace_count += line.count('{') - line.count('}')
                        if brace_count == 0 and json_start != -1:
                            json_end = i
                            break
                
                if json_start != -1 and json_end != -1:
                    json_str = '\n'.join(lines[json_start:json_end + 1])
                    json_data = json.loads(json_str)
            
            # If we couldn't parse JSON, return error in expected format
            if not json_data:
                return '''```json
{
  "thinking": "Error: Could not parse response as valid JSON",
  "verdict_last_action": "Unable to parse response",
  "image_observation": "",
  "memory": "",
  "current_goal": "",
  "action": {}
}
```'''
            
            # Ensure all required fields are present
            required_fields = ["thinking", "verdict_last_action", "image_observation", 
                             "memory", "current_goal", "action"]
            
            for field in required_fields:
                if field not in json_data:
                    json_data[field] = "" if field != "action" else {}
            
            # Format the normalized JSON with proper markdown wrapper
            normalized_json = json.dumps(json_data, indent=2, ensure_ascii=False)
            return f"```json\n{normalized_json}\n```"
            
        except Exception as e:
            # If any error occurs, return a properly formatted error response
            return '''```json
{
  "thinking": "Error: Failed to normalize response - ''' + str(e) + '''",
  "verdict_last_action": "Error in response processing",
  "image_observation": "",
  "memory": "",
  "current_goal": "",
  "action": {}
}
```'''
    
    @staticmethod
    def format_response(normalized_response: str) -> str:
        """Format normalized JSON response into readable terminal output with emojis"""
        try:
            # Extract JSON from markdown code block (we know it's properly formatted now)
            json_match = re.search(r'```json\s*(.*?)\s*```', normalized_response, re.DOTALL)
            if not json_match:
                return normalized_response  # Return as-is if no JSON found
            
            json_str = json_match.group(1)
            data = json.loads(json_str)
            
            # Build formatted output
            lines = []
            for field, emoji_label in AgentResponseFormatter.FIELD_EMOJIS.items():
                if field in data:
                    value = data[field]
                    # Convert dict/list to string for action field
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value, indent=2)
                    
                    lines.append(f"- {emoji_label}: {value}")
            
            return "\n".join(lines)
            
        except Exception:
            # If any error, return original response
            return normalized_response