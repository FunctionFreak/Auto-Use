# Copyright 2026 Ashish Yadav — Auto-Use

import os
import json
import re
import logging

logger = logging.getLogger(__name__)

class DomainKnowledgeService:
    """Service for injecting domain-specific knowledge based on browser URL or OS app"""
    
    def __init__(self):
        """Initialize and load domain knowledge mappings"""
        # The LIVE skills the user can edit live in autouse_data/skills/mac/,
        # outside the install folder. Fall back to the .md files shipped beside
        # this module if that can't be resolved for any reason.
        try:
            from Auto_Use import skills_dir
            self.current_dir = str(skills_dir("mac"))
        except Exception:
            self.current_dir = os.path.dirname(os.path.abspath(__file__))
        self.mappings = self._load_mappings()
        
        # Browser detection keywords
        self.browser_keywords = ["chrome", "firefox", "edge", "opera", "brave", "safari", "vivaldi", "browser"]
    
    def _load_mappings(self) -> dict:
        """Load skills.json mapping file"""
        try:
            json_path = os.path.join(self.current_dir, "skills.json")
            if os.path.exists(json_path):
                with open(json_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            else:
                logger.warning("skills.json not found")
                return {"browser": {}, "os": {}}
        except Exception as e:
            logger.error(f"Error loading skills.json: {str(e)}")
            return {"browser": {}, "os": {}}
    
    def _is_browser(self, application_name: str) -> bool:
        """Check if the application is a web browser"""
        app_lower = application_name.lower()
        return any(keyword in app_lower for keyword in self.browser_keywords)
    
    def _extract_url(self, element_tree: str) -> str:
        """Extract the browser URL from the element tree (scheme-optional).

        macOS browsers commonly display the address scheme-stripped
        (e.g. ``github.com/...``), and address-bar field names vary per browser,
        so we scan every ``valuePattern.value`` field: prefer one whose name looks
        like an address/search/url/location bar, then fall back to any value that
        looks like a URL or bare domain. A non-URL result is harmless — it only
        loads a skill if it matches a known domain in skills.json.
        """
        try:
            row_pattern = r'<element name="([^"]*)"[^>]*valuePattern\.value="([^"]*)"'

            candidate = ""
            for m in re.finditer(row_pattern, element_tree):
                name, value = m.group(1), m.group(2)
                if re.search(r'address|search bar|\burl\b|location', name, re.IGNORECASE):
                    if self._looks_like_url(value):
                        return value
                    candidate = candidate or value  # omnibox-named but odd value; hold

            if candidate:
                return candidate

            # Fallback: any value that looks like a URL / bare domain.
            for m in re.finditer(r'valuePattern\.value="([^"]*)"', element_tree):
                if self._looks_like_url(m.group(1)):
                    return m.group(1)

            return ""
        except Exception as e:
            logger.error(f"Error extracting URL: {str(e)}")
            return ""

    def _looks_like_url(self, value: str) -> bool:
        """Heuristic: does this field value look like a URL or bare domain?"""
        v = value.strip()
        if not v or " " in v:
            return False
        if v.startswith("http://") or v.startswith("https://"):
            return True
        # bare hostname with at least one dot, optional port/path/query/fragment
        return bool(re.match(r'^[a-zA-Z0-9\-]+(\.[a-zA-Z0-9\-]+)+([/:?#].*)?$', v))
    
    def _normalize_url(self, url: str) -> str:
        """Strip protocol (https://, http://) from URL for comparison"""
        url = url.strip()
        if url.startswith("https://"):
            return url[8:]
        elif url.startswith("http://"):
            return url[7:]
        return url
    
    def _match_browser_pattern(self, url: str) -> str:
        """Match URL against browser patterns, return .md filename or empty string"""
        if not url:
            return ""
        
        browser_patterns = self.mappings.get("browser", {})
        normalized_url = self._normalize_url(url)
        
        # Extract just the domain from the URL
        domain = normalized_url.split('/')[0]
        
        best_match = ""
        best_length = 0
        
        for pattern, md_file in browser_patterns.items():
            normalized_pattern = self._normalize_url(pattern)
            
            # Check if domain ends with the pattern (handles subdomains)
            if domain.endswith(normalized_pattern) or domain == normalized_pattern:
                if len(normalized_pattern) > best_length:
                    best_match = md_file
                    best_length = len(normalized_pattern)
        
        return best_match
    
    def _match_os_pattern(self, application_name: str) -> str:
        """Match application name against OS patterns, return .md filename or empty string"""
        os_patterns = self.mappings.get("os", {})
        
        app_lower = application_name.lower()
        
        for app_pattern, md_file in os_patterns.items():
            if app_pattern.lower() in app_lower:
                return md_file
        
        return ""
    
    def _load_knowledge_file(self, filename: str) -> str:
        """Load content from .md knowledge file"""
        try:
            file_path = os.path.join(self.current_dir, filename)
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    return f.read().strip()
            else:
                logger.warning(f"Knowledge file not found: {filename}")
                return ""
        except Exception as e:
            logger.error(f"Error loading knowledge file {filename}: {str(e)}")
            return ""
    
    def get_knowledge(self, application_name: str, element_tree: str) -> str:
        """
        Main method: Get browser guidelines (with optional nested domain knowledge) or standalone domain knowledge
        
        Args:
            application_name: Current application name from scanner
            element_tree: Element tree text from scanner
            
        Returns:
            Formatted <browser_guidelines> (with nested <domain_knowledge> if URL matches) or standalone <domain_knowledge>, or empty string
        """
        try:
            is_browser = self._is_browser(application_name)
            
            if is_browser:
                # Load browser.md via OS pattern match
                browser_md = self._match_os_pattern(application_name)
                browser_content = self._load_knowledge_file(browser_md) if browser_md else ""
                
                # Check URL for domain-specific knowledge
                domain_block = ""
                url = self._extract_url(element_tree)
                if url:
                    domain_md = self._match_browser_pattern(url)
                    if domain_md:
                        context = domain_md.replace(".md", "")
                        domain_content = self._load_knowledge_file(domain_md)
                        if domain_content:
                            domain_block = f'<domain_knowledge="{context}">\n{domain_content}\n</domain_knowledge>'
                
                # Build nested structure
                if browser_content:
                    inner = browser_content
                    if domain_block:
                        inner += f'\n{domain_block}'
                    return f'<browser_guidelines>\n{inner}\n</browser_guidelines>'
                
                return ""
            
            # Non-browser: standalone OS domain match (future desktop apps)
            os_md = self._match_os_pattern(application_name)
            if os_md:
                context = os_md.replace(".md", "")
                content = self._load_knowledge_file(os_md)
                if content:
                    return f'<domain_knowledge="{context}">\n{content}\n</domain_knowledge>'
            
            return ""
            
        except Exception as e:
            logger.error(f"Error getting domain knowledge: {str(e)}")
            return ""