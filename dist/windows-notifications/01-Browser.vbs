Option Explicit
Dim shell, folder, command
Set shell = CreateObject("WScript.Shell")
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
command = Chr(34) & shell.ExpandEnvironmentStrings("%SystemRoot%\System32\wscript.exe") & Chr(34) & " " & Chr(34) & folder & "\launch-hidden.vbs" & Chr(34) & " " & Chr(34) & folder & "\SuperMonoAgent\SuperMonoAgent.exe" & Chr(34) & " browser --workspace"
shell.Run command, 0, False
