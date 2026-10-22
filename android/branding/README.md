# Android launcher artwork

`launcher-master.png` is the selected ImageGen artwork created for SuperMonoAgent: a mint terminal chevron/cursor connected to remote device nodes on graphite. The original is retained in the local Codex generated-images directory; this copy is the project source.

Android resources contain 48/72/96/144/192 px legacy variants and a 432 px adaptive foreground. The mark stays inside the adaptive icon safe area. `android/res/mipmap-anydpi-v26/ic_launcher.xml` and the manifest use this icon for standard and round launcher entries.

Launcher artwork is a native resource. USB HTML/CSS/JS updates cannot replace the installed launcher icon; a signed APK upgrade is required.
