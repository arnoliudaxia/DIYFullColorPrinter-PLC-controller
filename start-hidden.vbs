Option Explicit

' Run the existing uv launcher without a console. Do not start the PLC GUI here
' directly: start.bat remains responsible for uv discovery and exit codes.
Dim shell, files, root, launcher, logPath, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
launcher = files.BuildPath(root, "start.bat")
logPath = files.BuildPath(root, "launcher.log")

If Not files.FileExists(launcher) Then
    MsgBox "Launcher not found: " & launcher, vbCritical, "PLC launcher"
    WScript.Quit 1
End If

shell.CurrentDirectory = root
command = Quoted(shell.ExpandEnvironmentStrings("%ComSpec%")) & " /d /s /c " & _
    Quoted(Quoted(launcher) & " --hidden > " & Quoted(logPath) & " 2>&1")

On Error Resume Next
exitCode = shell.Run(command, 0, True)
If Err.Number <> 0 Then
    MsgBox "Unable to start PLC: " & Err.Description, vbCritical, "PLC launcher"
    WScript.Quit 1
End If
On Error GoTo 0

If exitCode <> 0 Then
    MsgBox "PLC exited with code " & exitCode & "." & vbCrLf & _
        "See the startup log: " & logPath, vbCritical, "PLC launcher"
End If
WScript.Quit exitCode

Function Quoted(value)
    Quoted = Chr(34) & value & Chr(34)
End Function
