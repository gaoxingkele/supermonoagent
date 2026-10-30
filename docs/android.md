# Android mobile client

The main connection page accepts relay IP/address and a separate port (HTTPS 443 by default); a complete HTTP(S) URL remains supported. Tokens are remembered by default with endpoint-bound Android Keystore encryption. Changing the endpoint or port clears the old autofill, and a temporary decrypt failure never deletes a saved credential. Manual room entry is in advanced settings.

“测试连接” queries all rooms authorized by the Token and selects them by default. “连接” opens the chosen rooms. New connections explicitly opt into continued room discovery: new authorized rooms are included, while rooms unchecked during setup stay excluded. Older saved configurations that specified a fixed room set retain that narrower behavior. These native address/port controls require the new APK; a UI-only update cannot add them to an older Java shell.

The latest build is listed in [distribution artifacts](../dist/README.md). It maintains independent saved-host directory connections, renders/subscribes only the active terminal, isolates drafts and input mode by host/session, and closes transports while backgrounded. Changing endpoint or room clears the prior authority's token and session state. It also supports authorized known-room discovery; see [security and deployment](global-workspace-security.md). Build availability does not imply physical fault-recovery acceptance; see [the current verification record](todo-completion.md).

Build it with `scripts/build-android.ps1 -OutputDir dist/android/<next-version>`; each build increments the manifest version. Use that same numeric version for Windows. Generic builds contain no personal connection defaults. Device installation and fault-recovery status are recorded in [the current verification record](todo-completion.md). The production app has received an API2 shell update while retaining its data. A later isolated QA recovery build was rejected by the phone installer; that build's physical throw/hang recovery remains unverified.

The Android application is a small Java shell around the device's hardware-accelerated **Android System WebView**. The APK packages a dedicated mobile terminal document and the existing xterm dependencies. It connects directly by WebSocket to the configured daemon/relay using the existing protocol; it adds no extra relay hop. No Chromium engine is bundled. Android 8 (API 26) or newer is required; keep Android System WebView updated through the device's supported update mechanism.

## Build on Windows

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -Bootstrap
```

The bootstrap downloads a portable Eclipse Temurin JDK 21 and Google's Android command-line tools into ignored `.omx/tools`, verifies SHA-256 checksums, and installs `platforms;android-35`, `build-tools;35.0.0`, and `platform-tools`. It accepts the SDK licenses within this local SDK for the requested build. The build also uses the project Python environment and its standard-library ZIP writer to preserve Android-readable asset headers. Android Studio and Gradle are not required and no machine-wide environment variables are changed. Later builds omit `-Bootstrap`.

Build outputs:

- `dist/android/SuperMonoAgent.apk`: locally debug-signed, installable APK.
- `dist/android/SuperMonoAgent.apk.sha256`: APK checksum.
- `dist/android/apksigner-verification.txt`: signature verification report.
- `dist/android/packaged-assets.json`: SHA-256 hashes of the exact packaged local UI and vendor assets.
- `dist/android/toolchain.txt`: exact local JDK and Android tool versions, with JDK download/checksum metadata.

The debug keystore stays in `.omx/tools/android-debug.keystore`. Keep it to install updates over the existing debug app. This is a trial build, not a Play Store release; release signing and a protected release key are separate work. Build steps are reproducible; byte-identical APKs are not promised because ZIP/signing metadata and the generated local debug key vary.

## Connect

1. Install the APK on your Android device. No device is modified by the build script.
2. Enter the HTTP(S) **base URL** of your reachable daemon or relay, for example your own HTTPS endpoint; do not append `/workspace` or a query string.
3. Enter the Token if it is not already filled; “保存 Token” is checked by default. Tap “测试连接” to list authorized online rooms, all initially checked. Select the rooms to connect, choose `auto`, `dom`, or `webgl`, and connect. Unchecked rooms are not joined by automatic discovery.
4. Use the mobile page settings/reconnect controls to return to native connection settings or reload the packaged page. Back first hides the keyboard; otherwise it asks before returning to settings. Server-side terminal processes keep running.
5. Backgrounding calls the trusted local page pause hook and foregrounding calls resume. Renderer-process failure presents explicit retry/settings controls; retry retains the in-memory configuration.

The URL, connection code, and renderer preference persist privately. Saved Tokens are encrypted with Android Keystore AES-GCM and bound to the normalized server endpoint; preferences contain ciphertext, never the plaintext Token. Returning to settings or reopening the app refills the matching credential. Unchecking Save removes that endpoint's saved credential. A user-requested private APK can prefill its device-specific defaults from an ignored operator file; keep that APK outside public dist. Public builds contain no personal credentials. The native host passes current credentials in a URI-encoded JSON fragment to the packaged page, never a remote URL query; the page consumes the fragment immediately. App backups are disabled.

The packaged terminal page uses the configured endpoint origin (scheme, host and port), under `/mobile/index.html`; WebView intercepts this URL and serves only APK bytes. Binding the document to the real endpoint origin keeps connection classification consistent, while matching HTTP/HTTPS schemes avoids mixed content. The origin normalizes default ports and lowercase host names to match Chromium serialization. This does not bypass local-network permission checks. Desktop Edge requires its own local-network permission when exercising intercepted mock pages; physical Android behavior remains unverified. No security flags or local-network checks are disabled. Android target 35 uses the existing INTERNET permission for local-network access; future target changes must review [Android local-network permission guidance](https://developer.android.com/privacy-and-security/local-network-permission). The `/mobile/` path is a reserved local UI path, independent of any endpoint base-path used for the WebSocket connection. The Activity restricts all document navigation to its packaged index, with main-frame local `/settings` and `/reload` actions. All page navigation outside the exact packaged index is blocked; same-origin server HTML and scripts are also blocked. An exact filename allowlist serves only the four mobile UI files and eight vendor dependencies; encoded paths, traversal, changed origins or ports, arbitrary assets and other subresources are rejected. No remote script is loaded and no native JavaScript interface is exposed. Lifecycle JavaScript runs only while the trusted packaged index is current.

File/content access, zoom controls and third-party cookies are disabled. Text zoom is fixed at 100%; Android 11+ uses explicit system-bar and keyboard insets, while Android 8?10 uses resize behavior. Certificate errors retain WebView's default rejection. Public connections require HTTPS/WSS; HTTP/WS is retained only for loopback development. Changing the endpoint loads only that server's saved credential.

## Compare mobile renderers

`dom` selects xterm's default non-WebGL renderer (the current xterm implementation uses canvas/DOM plumbing); `webgl` explicitly requests its GPU renderer for compatibility testing, and `auto` defaults to DOM on Android so device-specific WebGL font-atlas problems cannot distort or hide terminal text. Changing this preference reloads the same workspace and keeps the server session. System WebView is the browser engine; WebGL is a renderer within that engine, not an alternative browser engine. A forced WebGL request may fall back when the device cannot create a WebGL context; use the workspace's displayed renderer to record the actual result.

For each device and renderer, compare the same PowerShell/CMD session, Chinese IME input, pasted Unicode, Ctrl/C/Esc/Tab/arrow soft keys, scrolling, keyboard open/close, portrait/landscape, reconnect after a network switch, and large output. Record Android/System WebView versions, latency and CPU on both daemon and relay. Compilation/signature checks cannot establish device touch/IME quality or mobile GPU compatibility; those require an emulator or physical phone.

Official tooling references: [Android command-line tools](https://developer.android.com/tools), [AAPT2](https://developer.android.com/tools/aapt2), [D8](https://developer.android.com/tools/d8), [APK signing](https://developer.android.com/tools/apksigner), [Adoptium API](https://adoptium.net/installation/ci-scripts).

## Implementation and verification boundary

The build stages `android/assets/mobile/{index.html,app.js,app.css,mobile-core.js}` plus existing `src/supermonoagent/static` xterm and terminal-client files into a temporary asset directory, then packages it with AAPT2. Vendor sources are not duplicated in the repository. The previous APK is preserved once under `.omx/android-v1/`. The current build is version code 60 / version name 0.2.58; each build increments both in `android/AndroidManifest.xml`. The six agent profile JSON files are staged from the same `src/supermonoagent/static/agent-modes/` source used by Windows.

`python -m unittest tests.test_android -v` compiles and runs the actual pure Java endpoint and asset policy against valid and adversarial URLs, and checks native security/lifecycle/build contracts. Android SDK compilation verifies actual platform API signatures. Neither substitutes for real Android keyboard, lifecycle, rotation or GPU tests.

Official WebView references: [load local content](https://developer.android.com/develop/ui/views/layout/webapps/load-local-content), [WebSettings](https://developer.android.com/reference/android/webkit/WebSettings), [WindowInsets](https://developer.android.com/reference/android/view/WindowInsets).

## Adaptive layout and USB updates

Current behavior, physical-device evidence, rendering fixes, connection testing, version policy, and USB hot-update/probe commands are documented in [Android live updates](android-live-updates.md). Default phone sizing waits for backend acknowledgment before changing the local grid; shared-original-size mode scales the server grid. The native homepage displays APK/build/UI versions and recent updates. USB UI revisions update without reinstalling the native APK. Internet OTA delivery is not included.
