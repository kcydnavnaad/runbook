import os

# Prompt the user to enter the search URL
search_url = input("Enter the URL to search for in the SSH configuration: ")

# Path to the SSH configuration file
ssh_config_file = os.path.expanduser("~/.ssh/config")

# Use grep to search for the URL in the SSH configuration file
grep_command = f"grep -in '{search_url}' '{ssh_config_file}'"
result = os.popen(grep_command).read()
match_line = None

if result:
    lines = result.split("\n")
    for line in lines:
        if line.strip():
            parts = line.split(":")
            match_line = int(parts[0])
            break

if match_line is not None:
    print(f"URL found in SSH configuration. Opening file with nano at line {match_line}...")
    os.system(f"nano +{match_line} '{ssh_config_file}'")
else:
    print("URL not found in SSH configuration.")

