# JARVIS Environment — sourced by all agents and shells
# Source in .bashrc: source ~/.jarvis-env.sh

# Display (for xdotool, xclip, browser automation)
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/turbo/.Xauthority}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/1000/bus}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"

# JARVIS Cluster
export JARVIS_HOME="/home/turbo/IA/Core/jarvis"
export JARVIS_CDP_URL="http://127.0.0.1:9222"
export JARVIS_BROWSEROS_CDP="http://127.0.0.1:9105"
export JARVIS_MCP_URL="http://127.0.0.1:8901/mcp"
export JARVIS_WS_URL="http://127.0.0.1:9742"

# AI Models
export LM_STUDIO_URL="http://127.0.0.1:1234"
export OLLAMA_HOST="http://127.0.0.1:11434"
export M3_URL="http://192.168.1.113:1234"
export M2_URL="http://192.168.1.26:1234"

# GPU
export CUDA_VISIBLE_DEVICES="0,1,2,3,4,5"
export NVIDIA_VISIBLE_DEVICES="all"

# Python paths
export PYTHONPATH="${JARVIS_HOME}:${JARVIS_HOME}/scripts:${PYTHONPATH}"

# Clipboard
export CLIPBOARD_TOOL="xclip"

# PATH
export PATH="${JARVIS_HOME}/scripts:${HOME}/.local/bin:${PATH}"
