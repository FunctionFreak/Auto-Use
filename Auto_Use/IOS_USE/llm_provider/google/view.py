"""View utilities for Google Gemini models"""

GEMINI_MODELS = {
    "gemini-2.5-flash": {
        "api_name": "gemini-2.5-flash",
        "display_name": "Gemini 2.5 Flash",
        "vision": True
    },
    "gemini-2.5-pro": {
        "api_name": "gemini-2.5-pro",
        "display_name": "Gemini Pro",
        "vision": True
    }
}

def get_model_info(model_short_name: str) -> dict:
    """Get model information from short name"""
    return GEMINI_MODELS.get(model_short_name, {
        "api_name": model_short_name,
        "display_name": model_short_name,
        "vision": True
    })

