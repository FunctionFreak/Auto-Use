import os
import json
import logging
from datetime import datetime

# Configure logger
logger = logging.getLogger(__name__)

class TaskTrackerService:
    def __init__(self):
        """Initialize the Task Tracker Service"""
        # Get the project root directory
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # Go up two levels from Auto_Use/IOS_USE/controller/task_tracker/ to reach IOS_USE/
        self.root_dir = os.path.dirname(os.path.dirname(current_dir))
        
        # Set todo directory path
        self.todo_dir = os.path.join(self.root_dir, "todo")
        self.todo_file = os.path.join(self.todo_dir, "todo.md")
        
        # Create todo directory if it doesn't exist
        self._ensure_todo_directory()
    
    def _ensure_todo_directory(self):
        """Create todo directory if it doesn't exist"""
        try:
            os.makedirs(self.todo_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"Error creating todo directory: {str(e)}")
            raise
    
    def save_todo(self, todo_content):
        """Save todo list content to markdown file"""
        try:
            # Check if file is new
            is_new_file = not os.path.exists(self.todo_file)
            
            # Write to file (overwrite mode)
            with open(self.todo_file, "w", encoding="utf-8") as f:
                f.write(todo_content)
            
            # Silent save - no terminal output
            return True
            
        except Exception as e:
            logger.error(f"Error saving todo list: {str(e)}")
            return False
    
    def update_task(self, update_string):
        """Update a task in the todo list by replacing the exact string"""
        try:
            # Read current todo content
            if not os.path.exists(self.todo_file):
                logger.warning("Todo file doesn't exist")
                return False
            
            with open(self.todo_file, "r", encoding="utf-8") as f:
                content = f.read()
            
            # Replace the task string
            # The update_string should be the complete line with [x] marking
            if update_string in content:
                # Already updated, nothing to do
                logger.info(f"Task already updated: {update_string}")
                return True
            
            # Try to find the unchecked version and replace it
            # Remove [x] from update_string to find the original
            original_string = update_string.replace("[x]", "[ ]", 1)
            
            if original_string in content:
                updated_content = content.replace(original_string, update_string)
                
                # Write back to file
                with open(self.todo_file, "w", encoding="utf-8") as f:
                    f.write(updated_content)
                
                logger.info(f"Updated task: {original_string} -> {update_string}")
                return True
            else:
                logger.warning(f"Task not found in todo list: {original_string}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating task: {str(e)}")
            return False