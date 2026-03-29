' Halo Launcher -- starts Halo without a visible console window
' Uses pythonw to suppress the console, with --debug for the preview window

Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

haloDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonExe = haloDir & "\.venv\Scripts\pythonw.exe"
haloScript = haloDir & "\halo.py"

' Use pythonw (no console) with --debug for the preview window
WshShell.CurrentDirectory = haloDir
WshShell.Run """" & pythonExe & """ """ & haloScript & """ --debug", 0, False
