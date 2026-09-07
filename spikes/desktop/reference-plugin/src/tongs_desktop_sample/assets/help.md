# Plugin inspector

This documentation is bundled in the independently installed Python plugin wheel.
Enter a message and select **Call Python plugin**. The response comes from this
plugin's Python backend and includes the number of calls in the current session.

The package also declares a terminal-only plugin. It remains usable in the TUI;
the desktop loader lists it as terminal-only without calling its TUI hooks.
This is a prototype API, not a stable extension contract.
