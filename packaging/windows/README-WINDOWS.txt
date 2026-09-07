PilferedParrot for Windows
==========================

This is the PilferedParrot 0.7.0-rc.1 Harness release-candidate preview for Windows 10/11 x64.
The ZIP is portable: extract it anywhere and run PilferedParrot.exe.
No Python installation or administrator rights are required.
Install Google Chrome, Chromium, or Microsoft Edge to open the interface.
This preview executable is unsigned. SHA256SUMS is available with the release.

The first run opens the local browser interface. Keep the console window open
while the app is running. Use PilferedParrot.exe --help for command-line
options. User state is stored under %LOCALAPPDATA%\PilferedParrot, and new
projects default to %USERPROFILE%\PilferedParrot Projects. On first launch the
app generates %LOCALAPPDATA%\PilferedParrot\config.json. Edit that generated
file to customize providers while preserving its web.chat_store and ledger
paths. The bundled config.example.json is a reference for provider options;
do not replace the generated config with it because its default paths are for
source development.

The package includes the PilferedParrot application and Python runtime. Provider
CLIs (Codex, Claude, Gemini, and Antigravity) and local Qwen are separate
software and must be installed/configured independently if you use them.
On Windows, the packaged preview supports the browser chat and file tools;
the Unix-only Bubblewrap shell is disabled. Provider CLIs are separate software
and are not validated by this package; supported npm shims require Node.js and
are resolved directly without cmd.exe.

This is a Windows x64 build. Keep the extracted directory together; do not
move or delete its internal files while the application is running.
Close the running application before extracting and launching an updated version.
