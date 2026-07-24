import json
import os
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)


class VaultService:
    """Service for managing and retrieving credentials"""
    
    def __init__(self):
        self.element_tree_text = ""
        self.credentials = {}
        self._load_credentials()
    
    def _load_credentials(self):
        """Load credentials from JSON file"""
        try:
            # autouse_data/vault/credentials.json — outside the install folder,
            # so uninstalling AutoUse can't delete the user's credentials.
            try:
                from Auto_Use import vault_file
                credentials_path = str(vault_file())
            except Exception:
                current_dir = os.path.dirname(os.path.abspath(__file__))
                credentials_path = os.path.join(current_dir, 'credentials.json')

            if os.path.exists(credentials_path):
                with open(credentials_path, 'r', encoding='utf-8') as f:
                    self.credentials = json.load(f)
                logger.info(f"Loaded credentials for {len(self.credentials)} applications")
            else:
                logger.debug("credentials.json not found")
                self.credentials = {}
        except Exception as e:
            logger.error(f"Error loading credentials: {str(e)}")
            self.credentials = {}
    
    def update_element_tree(self, element_tree_text):
        """Update the stored element tree"""
        self.element_tree_text = element_tree_text
        logger.debug("Vault element tree updated")
    
    def get_credential_for_element(self, element_number):
        """Get credential for a specific element based on app context and field type"""
        try:
            # Parse element tree to find app name and target element
            lines = self.element_tree_text.strip().split('\n')
            
            app_name = None
            target_element = None
            
            for line in lines:
                # Parse element info
                if f'[{element_number}]' in line:
                    target_element = self._parse_element_line(line)
                elif '[1]' in line and 'type="application"' in line:
                    # Element 1 is usually the application
                    app_info = self._parse_element_line(line)
                    if app_info:
                        app_name = app_info.get('label', '')
            
            if not app_name or not target_element:
                logger.error(f"Could not find app name or target element {element_number}")
                return None
            
            # Find matching credential
            credential = self._find_credential(app_name, target_element)
            return credential
            
        except Exception as e:
            logger.error(f"Error getting credential: {str(e)}")
            return None
    
    def _parse_element_line(self, line):
        """Parse element line to extract attributes"""
        try:
            # Extract attributes from format: [1]<type="button", label="Back", value="", x="0", y="0", w="100", h="50" />
            attrs = {}
            
            # Extract type
            if 'type="' in line:
                start = line.find('type="') + 6
                end = line.find('"', start)
                attrs['type'] = line[start:end]
            
            # Extract label
            if 'label="' in line:
                start = line.find('label="') + 7
                end = line.find('"', start)
                attrs['label'] = line[start:end]
            
            # Extract value
            if 'value="' in line:
                start = line.find('value="') + 7
                end = line.find('"', start)
                attrs['value'] = line[start:end]
            
            return attrs
        except Exception as e:
            logger.error(f"Error parsing element line: {str(e)}")
            return None
    
    def _find_credential(self, app_name, element_info):
        """Find matching credential using fuzzy matching"""
        # Clean up app name for matching
        app_name_clean = app_name.lower().replace(' ', '').strip()
        element_label = element_info.get('label', '').lower()
        element_type = element_info.get('type', '').lower()
        
        # Search through credentials
        for app_key, app_creds in self.credentials.items():
            app_key_clean = app_key.lower().replace(' ', '').strip()
            
            # Fuzzy match app name
            if (app_key_clean in app_name_clean or 
                app_name_clean in app_key_clean or
                self._fuzzy_match(app_key_clean, app_name_clean)):
                
                # Found matching app, now find field
                if 'email' in element_label or 'username' in element_label:
                    return app_creds.get('email') or app_creds.get('username')
                elif 'password' in element_label or element_type == 'securetextfield':
                    return app_creds.get('password')
                elif 'phone' in element_label:
                    return app_creds.get('phone')
                elif 'code' in element_label or 'pin' in element_label:
                    return app_creds.get('code') or app_creds.get('pin')
        
        logger.warning(f"No credential found for app: {app_name}, field: {element_label}")
        return None
    
    def _fuzzy_match(self, str1, str2):
        """Simple fuzzy matching for app names"""
        # Check if significant parts match
        if len(str1) < 3 or len(str2) < 3:
            return False
        
        # Check if one contains significant part of other
        if len(str1) > 4 and str1[:4] in str2:
            return True
        if len(str2) > 4 and str2[:4] in str1:
            return True
        
        return False


# Create global instance
vault_service = VaultService()