# Auto_Use/IOS_USE/controller/view.py

import json
import logging
import time
from .task_tracker.service import TaskTrackerService
from .service import controller_service
from .app import app_launcher_service

# Configure logger
logger = logging.getLogger(__name__)

class ControllerView:
    def __init__(self):
        """Initialize the Controller View - central router for iPhone actions"""
        self.task_tracker = TaskTrackerService()
        
    def route_action(self, action_data):
        """
        Route actions to appropriate handler based on action type
        Supports all actions defined in system prompt with proper naming
        
        Args:
            action_data (dict): The action dictionary from agent response
            
        Returns:
            dict: Result of the action execution
        """
        try:
            # Process multiple actions if present
            # Some actions can be combined (e.g., click + update_task)
            results = []
            
            # Handle TODO creation
            if "todo" in action_data:
                todo_content = action_data["todo"]
                success = self.task_tracker.save_todo(todo_content)
                
                result = {
                    "status": "success" if success else "error",
                    "action": "todo",
                    "message": "Todo list created successfully" if success else "Failed to create todo list"
                }
                
                if success:
                    logger.info("✓ Todo list created")
                else:
                    logger.error("✗ Failed to create todo list")
                
                if len(action_data) == 1:
                    return result
                results.append(result)
            
            # Handle UPDATE TASK
            if "update_task" in action_data:
                task_string = action_data["update_task"]
                success = self.task_tracker.update_task(task_string)
                
                result = {
                    "status": "success" if success else "error",
                    "action": "update_task",
                    "task": task_string
                }
                
                if success:
                    logger.info(f"✓ Task updated: {task_string}")
                else:
                    logger.error(f"✗ Failed to update task: {task_string}")
                    result["message"] = "Failed to update task"
                
                if len(action_data) == 1:
                    return result
                results.append(result)
            
            # Handle CLICK action
            if "click" in action_data:
                element_index = action_data["click"]
                
                try:
                    # Convert to int if string
                    element_num = int(element_index)
                    
                    controller_service.click(element_num)
                    
                    logger.info(f"✓ Clicked element {element_num}")
                    print(f"✅ Click executed successfully on element {element_num}")
                    
                    result = {
                        "status": "success",
                        "action": "click",
                        "element": element_num
                    }
                except Exception as e:
                    logger.error(f"✗ Click failed: {str(e)}")
                    print(f"❌ Click FAILED on element {element_index}: {str(e)}")
                    result = {
                        "status": "error",
                        "action": "click",
                        "element": element_index,
                        "message": str(e)
                    }
                
                if len(action_data) == 1:
                    return result
                results.append(result)
            
            # Handle TYPE action
            if "type" in action_data:
                type_data = action_data["type"]
                # type_data format: {"element_index": "value to type"}
                
                try:
                    # Extract element number and text
                    elem_num = list(type_data.keys())[0]
                    text_value = type_data[elem_num]
                    
                    # Convert element number to int
                    element_num = int(elem_num)
                    
                    controller_service.type_text(element_num, text_value)
                    logger.info(f"✓ Typed '{text_value}' in element {element_num}")
                    
                    result = {
                        "status": "success",
                        "action": "type",
                        "element": element_num,
                        "text": text_value
                    }
                except Exception as e:
                    logger.error(f"✗ Type failed: {str(e)}")
                    result = {
                        "status": "error",
                        "action": "type",
                        "data": type_data,
                        "message": str(e)
                    }
                
                if len(action_data) == 1:
                    return result
                results.append(result)
            
            # Handle OPEN_APP action (matching system prompt naming)
            if "open_app" in action_data:
                app_name = action_data["open_app"]
                
                # Handle special 'home' case
                if app_name.lower() == 'home':
                    try:
                        success = app_launcher_service.go_home()
                        if success:
                            logger.info("✓ Navigated to home screen")
                            result = {
                                "status": "success",
                                "action": "open_app",
                                "app_name": "home",
                                "message": "Navigated to home screen"
                            }
                        else:
                            result = {
                                "status": "error",
                                "action": "open_app",
                                "app_name": "home",
                                "message": "Failed to navigate to home screen"
                            }
                    except Exception as e:
                        logger.error(f"✗ Go home failed: {str(e)}")
                        result = {
                            "status": "error",
                            "action": "open_app",
                            "app_name": "home",
                            "message": str(e)
                        }
                else:
                    # Handle regular app launch
                    try:
                        success = app_launcher_service.launch_app(app_name)
                        if success:
                            logger.info(f"✓ Launched app: {app_name}")
                            result = {
                                "status": "success",
                                "action": "open_app",
                                "app_name": app_name,
                                "message": f"Successfully launched {app_name}"
                            }
                        else:
                            result = {
                                "status": "error",
                                "action": "open_app",
                                "app_name": app_name,
                                "message": f"Failed to launch {app_name} - app not found or not installed"
                            }
                    except Exception as e:
                        logger.error(f"✗ App launch failed: {str(e)}")
                        result = {
                            "status": "error",
                            "action": "open_app",
                            "app_name": app_name,
                            "message": str(e)
                        }
                
                if len(action_data) == 1:
                    return result
                results.append(result)
            
            # Handle VAULT action
            if "vault" in action_data:
                element_number = action_data["vault"]
                
                try:
                    # Get credential from vault
                    from ..vault.service import vault_service
                    credential_value = vault_service.get_credential_for_element(int(element_number))
                    
                    if credential_value:
                        # Use existing type_text to fill the credential
                        controller_service.type_text(element_number, credential_value)
                        logger.info(f"🔐 Vault filled element {element_number}")
                        
                        result = {
                            "status": "success",
                            "action": "vault",
                            "element": element_number,
                            "message": "Credential filled successfully"
                        }
                    else:
                        logger.error(f"No credential found for element {element_number}")
                        result = {
                            "status": "error",
                            "action": "vault",
                            "element": element_number,
                            "message": "No matching credential found"
                        }
                except Exception as e:
                    logger.error(f"Vault action failed: {str(e)}")
                    result = {
                        "status": "error",
                        "action": "vault",
                        "element": element_number,
                        "message": str(e)
                    }
                
                if len(action_data) == 1:
                    return result
                results.append(result)
            
            # Handle ELEMENT_SCROLL action
            if "element_scroll" in action_data:
                scroll_data = action_data["element_scroll"]
                
                try:
                    # Parse scroll data - Format from system prompt: {"element_number": "direction"}
                    # Example: {"3": "left"} or {"9": "up"}
                    if isinstance(scroll_data, dict):
                        # Extract element number (key) and direction (value)
                        element_num = int(list(scroll_data.keys())[0])
                        direction = scroll_data[str(element_num)]
                        
                        controller_service.scroll(element_num, direction)
                        logger.info(f"✓ Scrolled element {element_num} {direction}")
                        
                        result = {
                            "status": "success",
                            "action": "element_scroll",
                            "element": element_num,
                            "direction": direction
                        }
                    else:
                        # String or invalid format
                        logger.error(f"Invalid scroll format: {scroll_data}")
                        result = {
                            "status": "error",
                            "action": "element_scroll",
                            "message": "Invalid scroll format. Expected: {\"element_number\": \"direction\"}"
                        }
                except Exception as e:
                    logger.error(f"✗ Scroll failed: {str(e)}")
                    result = {
                        "status": "error",
                        "action": "element_scroll",
                        "data": scroll_data,
                        "message": str(e)
                    }
                
                if len(action_data) == 1:
                    return result
                results.append(result)

            # Handle VIDEO_PLAYER action
            if "video_player" in action_data:
                command = action_data["video_player"]
                
                try:
                    if command == "close":
                        success = controller_service.video_close()
                        tool_response = "videoplayer: Result - Closed" if success else "videoplayer: Result - Failed to close"
                    elif command == "streaming":
                        is_streaming = controller_service.video_check_streaming()
                        tool_response = "videoplayer: Result - Streaming" if is_streaming else "videoplayer: Result - Not streaming"
                    elif command == "pause":
                        success = controller_service.video_pause()
                        tool_response = "videoplayer: Result - Paused" if success else "videoplayer: Result - Failed to pause"
                    elif command == "play":
                        success = controller_service.video_play()
                        tool_response = "videoplayer: Result - Playing" if success else "videoplayer: Result - Failed to play"
                    else:
                        raise ValueError(f"Unknown video_player command: {command}")
                    
                    result = {
                        "status": "success",
                        "action": "video_player",
                        "command": command,
                        "tool_response": tool_response
                    }
                    logger.info(f"🎬 {tool_response}")
                    print(f"📤 Video player response: {tool_response}")
                    
                except Exception as e:
                    logger.error(f"Video player action failed: {str(e)}")
                    result = {
                        "status": "error",
                        "action": "video_player",
                        "command": command,
                        "message": str(e),
                        "tool_response": f"videoplayer: Result - Error: {str(e)}"
                    }
                
                if len(action_data) == 1:
                    return result
                results.append(result)
            
            # Handle WAIT action - return duration for service.py to handle before next scan
            if "wait" in action_data:
                seconds = int(action_data["wait"])
                logger.info(f"⏳ Will wait {seconds} seconds before next scan")
                
                result = {
                    "status": "success",
                    "action": "wait",
                    "seconds": seconds,
                    "message": f"Will wait {seconds} seconds before next scan"
                }
                
                if len(action_data) == 1:
                    return result
                results.append(result)
            
            # Handle DONE action - signals task completion
            if "done" in action_data:
                summary = action_data["done"]
                logger.info(f"✅ Task completed - Summary: {summary}")
                
                result = {
                    "status": "success",
                    "action": "done",
                    "summary": summary,
                    "message": "Task completed successfully"
                }
                
                return result  # Always return done immediately
            
            # If we processed multiple actions, return combined results
            if results:
                if len(results) == 1:
                    return results[0]
                else:
                    # Check if any result has a tool_response and promote it to top level
                    combined_result = {
                        "status": "success",
                        "action": "combined",
                        "results": results,
                        "message": f"Executed {len(results)} actions"
                    }
                    # Promote tool_response to top level if present in any result
                    for result in results:
                        if result.get('tool_response'):
                            combined_result['tool_response'] = result['tool_response']
                            break
                    return combined_result
            else:
                return {
                    "status": "error",
                    "message": "No valid action found in action data",
                    "received": action_data
                }
                
        except Exception as e:
            logger.error(f"Error routing action: {str(e)}")
            return {
                "status": "error",
                "message": f"Exception in route_action: {str(e)}",
                "action_data": action_data
            }
    
    def get_todo_status(self):
        """
        Get the current todo list content
        
        Returns:
            str: Todo list content or None if not found
        """
        try:
            import os
            if os.path.exists(self.task_tracker.todo_file):
                with open(self.task_tracker.todo_file, 'r', encoding='utf-8') as f:
                    return f.read()
            return None
        except Exception as e:
            logger.error(f"Error reading todo status: {str(e)}")
            return None