from e2b import Template

# Define the GrindBot sandbox template using the V2 builder API.
# Each run_cmd() becomes a cached layer — reordering them busts the cache.
# E2B's ubuntu base runs as non-root 'user', so privileged commands need sudo.
template = (
    Template()
    .from_ubuntu_image("22.04")
    .run_cmd(
        "sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "curl git python3 python3-pip ca-certificates "
        "&& sudo rm -rf /var/lib/apt/lists/*"
    )
    .run_cmd(
        "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash - "
        "&& sudo apt-get install -y nodejs "
        "&& sudo rm -rf /var/lib/apt/lists/*"
    )
    # Gemini CLI — pre-installed so sandboxes start ready with zero install time
    .run_cmd("sudo npm install -g @google/gemini-cli")
    # Git config for non-interactive commits inside the VM
    .run_cmd(
        'git config --global user.email "grindbot@sandbox" '
        '&& git config --global user.name "GrindBot" '
        "&& git config --global init.defaultBranch main"
    )
)
