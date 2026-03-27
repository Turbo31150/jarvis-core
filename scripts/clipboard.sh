#!/bin/bash
# JARVIS Clipboard — Universal copy/paste for all agents
# Usage:
#   clipboard.sh copy "text to copy"
#   clipboard.sh paste                    → outputs clipboard content
#   clipboard.sh copy-file /path/to/file  → copies file content
#   clipboard.sh copy-selection           → copies X11 selection
#   clipboard.sh type "text"              → types text via xdotool

case "$1" in
  copy)
    echo -n "$2" | xclip -selection clipboard
    echo "OK: copied ${#2} chars"
    ;;
  paste)
    xclip -selection clipboard -o 2>/dev/null
    ;;
  copy-file)
    xclip -selection clipboard < "$2"
    echo "OK: copied from $2"
    ;;
  copy-selection)
    xclip -selection primary -o 2>/dev/null
    ;;
  type)
    xdotool type --clearmodifiers "$2"
    echo "OK: typed ${#2} chars"
    ;;
  key)
    xdotool key "$2"
    echo "OK: pressed $2"
    ;;
  ctrl-c)
    xdotool key --clearmodifiers ctrl+c
    sleep 0.1
    xclip -selection clipboard -o 2>/dev/null
    ;;
  ctrl-v)
    xdotool key --clearmodifiers ctrl+v
    echo "OK: pasted"
    ;;
  ctrl-a)
    xdotool key --clearmodifiers ctrl+a
    echo "OK: select all"
    ;;
  *)
    echo "Usage: clipboard.sh {copy|paste|copy-file|copy-selection|type|key|ctrl-c|ctrl-v|ctrl-a} [args]"
    exit 1
    ;;
esac
