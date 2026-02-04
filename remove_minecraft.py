import json
import sys

# Read the JSON file
with open("docker/nginx/http-external-drop-in.conf", "r") as f:
    data = json.load(f)

# Function to filter out minecraft-related blocks
def filter_minecraft(obj):
    if isinstance(obj, dict):
        # Check if this is an "if" directive with minecraft references
        if (obj.get("directive") == "if" and 
            obj.get("args") and 
            len(obj.get("args", [])) >= 3 and
            ("minecraft_map" in str(obj.get("args", [])) or 
             "minecraft_guest_map" in str(obj.get("args", [])))):
            return None  # Remove this block
        
        # Recursively filter nested objects
        return {k: filter_minecraft(v) for k, v in obj.items() if filter_minecraft(v) is not None}
    
    elif isinstance(obj, list):
        # Filter list items, removing None values
        filtered = [filter_minecraft(item) for item in obj]
        return [item for item in filtered if item is not None]
    
    else:
        return obj

# Filter the data
filtered_data = filter_minecraft(data)

# Write back to file
with open("docker/nginx/http-external-drop-in.conf", "w") as f:
    json.dump(filtered_data, f, indent=4)

print("✅ Minecraft blocks removed successfully")
