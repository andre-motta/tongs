# Example dashboard

This separately installable plugin is a deterministic SDK example. It renders
three fixed review rows and summary counts, then invokes the Python provider's
`refresh` method when the button is pressed.

The example makes no forge calls, network requests, dynamic code evaluation, or
remote asset loads. Installed Python and UI plugins are trusted code. The
manifest and scoped host API reduce accidental cross-plugin access, but they do
not sandbox a malicious installed extension.
