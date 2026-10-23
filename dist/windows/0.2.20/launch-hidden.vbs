Option Explicit
Dim shell, args, command, index, result
Set shell = CreateObject("WScript.Shell")
Set args = WScript.Arguments
If args.Count = 0 Then WScript.Quit 2
command = ""
For index = 0 To args.Count - 1
    If index > 0 Then command = command & " "
    command = command & QuoteArgument(args(index))
Next
On Error Resume Next
result = shell.Run(command, 0, True)
If Err.Number <> 0 Then
    shell.Popup "SuperMonoAgent could not start. Run its console command for details.", 0, "SuperMonoAgent", 16
    WScript.Quit 1
End If
On Error GoTo 0
If result <> 0 Then
    shell.Popup "SuperMonoAgent exited with code " & result & ". Run its console command for details.", 0, "SuperMonoAgent", 16
End If
WScript.Quit result

Function QuoteArgument(value)
    Dim output, slashes, position, character
    output = Chr(34)
    slashes = 0
    For position = 1 To Len(value)
        character = Mid(value, position, 1)
        If character = "\" Then
            slashes = slashes + 1
        ElseIf character = Chr(34) Then
            output = output & String(slashes * 2 + 1, "\") & Chr(34)
            slashes = 0
        Else
            output = output & String(slashes, "\") & character
            slashes = 0
        End If
    Next
    QuoteArgument = output & String(slashes * 2, "\") & Chr(34)
End Function
