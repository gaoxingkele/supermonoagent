Option Explicit
Dim shell, fso, folder, destination, executable, scriptHost, entries, entry, launcher, linkPath, shortcut
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
executable = fso.BuildPath(folder, "SuperMonoAgent\SuperMonoAgent.exe")
scriptHost = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\wscript.exe")
If WScript.Arguments.Named.Exists("destination") Then
    destination = fso.GetAbsolutePathName(WScript.Arguments.Named("destination"))
Else
    If WScript.Arguments.Count <> 0 Then
        WScript.Echo "Usage: create-desktop-shortcuts.vbs [/destination:existing-folder]"
        WScript.Quit 2
    End If
    destination = shell.SpecialFolders("Desktop")
End If
If Not fso.FolderExists(destination) Or Not fso.FileExists(executable) Then
    WScript.Echo "Run this helper from the extracted SuperMonoAgent package. The destination must already exist."
    WScript.Quit 2
End If
entries = Array(Array("SuperMonoAgent Workspace", "02-Orca-Workspace.vbs"), Array("SuperMonoAgent Browser", "01-Browser.vbs"))
' Preflight both entries before changing any shortcut. Never replace unrelated links.
For Each entry In entries
    launcher = fso.BuildPath(folder, entry(1))
    If Not fso.FileExists(launcher) Then
        WScript.Echo "Missing launcher: " & launcher
        WScript.Quit 2
    End If
    linkPath = fso.BuildPath(destination, entry(0) & ".lnk")
    If fso.FileExists(linkPath) Then
        Set shortcut = shell.CreateShortcut(linkPath)
        If shortcut.Description <> entry(0) Then
            WScript.Echo "An unrelated shortcut already uses this name: " & linkPath
            WScript.Quit 3
        End If
    End If
Next
For Each entry In entries
    launcher = fso.BuildPath(folder, entry(1))
    linkPath = fso.BuildPath(destination, entry(0) & ".lnk")
    Set shortcut = shell.CreateShortcut(linkPath)
    shortcut.TargetPath = scriptHost
    shortcut.Arguments = "//nologo " & Chr(34) & launcher & Chr(34)
    shortcut.WorkingDirectory = folder
    shortcut.IconLocation = executable & ",0"
    shortcut.Description = entry(0)
    shortcut.WindowStyle = 7
    shortcut.Save
Next
WScript.Echo "SuperMonoAgent shortcuts created. If the package moves, run this helper again."
